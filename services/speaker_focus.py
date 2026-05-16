"""Active-speaker face tracking. Computes per-job focus track JSON.

Track is a list of {t, cx, cy} normalized [0,1] over source video time.
Crop width/height derived at render time from target aspect ratio.
"""
import json
import math
import os
import subprocess
from pathlib import Path
from typing import List, Optional
from services.media_tools import ffmpeg_path, ffprobe_path

_SAMPLE_FPS = 2.0
_EMA_ALPHA = 0.08
_DEADBAND = 0.015  # normalized; skip micro-moves smaller than this

# Face-focused mode params (independent of speaker mode).
_FACE_SAMPLE_FPS = 10.0
_FACE_MIN_CONFIDENCE = 0.5


def focus_track_path(job_id: str) -> Path:
    storage = os.getenv("STORAGE_PATH", "./storage")
    return Path(storage) / "jobs" / job_id / "focus_track.json"


def face_track_path(job_id: str) -> Path:
    storage = os.getenv("STORAGE_PATH", "./storage")
    return Path(storage) / "jobs" / job_id / "face_track.json"


def load_focus_track(job_id: str) -> Optional[List[dict]]:
    p = focus_track_path(job_id)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def load_face_track(job_id: str) -> Optional[List[dict]]:
    p = face_track_path(job_id)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def compute_focus_track(job_id: str, progress_callback=None) -> List[dict]:
    """Sample frames, detect faces, score by size + lip motion, smooth, save."""
    existing = load_focus_track(job_id)
    # Only treat non-empty cached track as valid; empty list = stale fallback, recompute.
    if existing:
        if progress_callback:
            progress_callback(100)
        return existing

    storage = os.getenv("STORAGE_PATH", "./storage")
    source = Path(storage) / "jobs" / job_id / "original.mp4"
    if not source.exists():
        return []

    try:
        import cv2
    except ImportError:
        _save_track(job_id, [])
        return []

    _, _, duration = _probe_video(str(source))
    if duration <= 0:
        _save_track(job_id, [])
        return []

    # Single-speaker shortcut: skip face detection entirely. Static center-upper
    # crop is plenty for solo talking-head content and saves the expensive
    # frame extract + Haar pass.
    try:
        from services.diarizer import load_diarization
        _turns = load_diarization(job_id) or []
    except Exception:
        _turns = []
    _unique_speakers = {t.get("speaker") for t in _turns if t.get("speaker")}
    if _turns and len(_unique_speakers) == 1:
        static_track = [
            {"t": 0.0, "cx": 0.5, "cy": 0.4, "fh": 0.22},
            {"t": float(round(duration, 3)), "cx": 0.5, "cy": 0.4, "fh": 0.22},
        ]
        _save_track(job_id, static_track)
        if progress_callback:
            progress_callback(100)
        return static_track

    frames_dir = Path(storage) / "jobs" / job_id / "_focus_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(frames_dir / "f_%06d.jpg")

    # Extract sampled frames once.
    subprocess.run(
        [ffmpeg_path(), "-y", "-i", str(source), "-vf", f"fps={_SAMPLE_FPS},scale=480:-2",
         "-q:v", "4", pattern],
        check=True, capture_output=True,
    )

    # Use OpenCV Haar cascade (ships with cv2, no model download needed).
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    face_cascade = cv2.CascadeClassifier(cascade_path)
    profile_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")

    frame_files = sorted(frames_dir.glob("f_*.jpg"))
    if not frame_files:
        _save_track(job_id, [])
        return []

    # Persistent face state: keyed by stable face ID (matched by proximity each frame).
    # face_state[fid] = {"lip_crop": ndarray, "cx": float, "cy": float}
    face_state: dict = {}
    next_fid = 0

    # Hysteresis: current tracked speaker face ID, pending challenger.
    active_fid = None
    challenger_fid = None
    challenger_streak = 0
    _SWITCH_FRAMES = 3   # frames challenger must dominate before we switch

    raw_track = []
    # Per-frame face record for post-pass diarization mapping.
    # frame_records[i] = {"t": float, "faces": [{fid, cx, cy, fh, lip}]}
    frame_records: list = []

    for idx, fpath in enumerate(frame_files):
        t = idx / _SAMPLE_FPS
        img = cv2.imread(str(fpath))
        if img is None:
            continue
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        rects = face_cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(30, 30))
        if len(rects) == 0:
            rects = profile_cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(30, 30))

        # Build candidate face list with normalized coords.
        candidates = []
        for (rx, ry, rw, rh) in rects:
            fx = rx / w
            fy = ry / h
            fw = rw / w
            fh = rh / h
            if fw < 0.04 or fh < 0.04:
                continue
            candidates.append({"cx": fx + fw / 2, "cy": fy + fh / 2, "fw": fw, "fh": fh,
                                "fx": fx, "fy": fy})

        if not candidates:
            if progress_callback and idx % 20 == 0:
                progress_callback(int(80 * idx / max(1, len(frame_files))))
            continue

        # Match candidates to known face IDs by nearest centroid.
        used_fids = set()
        matched: list = [None] * len(candidates)  # candidate_idx -> fid
        for ci, cand in enumerate(candidates):
            best_fid, best_dist = None, 0.15  # max match distance (norm coords)
            for fid, fs in face_state.items():
                if fid in used_fids:
                    continue
                d = ((cand["cx"] - fs["cx"]) ** 2 + (cand["cy"] - fs["cy"]) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best_fid = fid
            if best_fid is not None:
                matched[ci] = best_fid
                used_fids.add(best_fid)

        # Assign new IDs for unmatched candidates.
        for ci in range(len(candidates)):
            if matched[ci] is None:
                matched[ci] = next_fid
                next_fid += 1

        # Score each face using stable ID for lip motion continuity.
        faces = []
        for ci, cand in enumerate(candidates):
            fid = matched[ci]
            prev_lip = face_state.get(fid, {}).get("lip_crop")
            lip_score = _lip_motion_score_stable(img, cand["fx"], cand["fy"],
                                                  cand["fw"], cand["fh"], prev_lip)
            size_score = cand["fw"] * cand["fh"]
            score = size_score * (1.0 + 2.0 * lip_score)
            faces.append({"fid": fid, "cx": cand["cx"], "cy": cand["cy"],
                          "fh": cand["fh"], "score": score,
                          "fx": cand["fx"], "fy": cand["fy"],
                          "fw": cand["fw"], "lip": lip_score})

        # Update face state (position + lip crop).
        for face in faces:
            fid = face["fid"]
            lip_crop = _extract_lip_crop(img, face["fx"], face["fy"], face["fw"], face["fh"])
            face_state[fid] = {"cx": face["cx"], "cy": face["cy"], "lip_crop": lip_crop}

        # Prune stale face IDs not seen this frame.
        seen_fids = {f["fid"] for f in faces}
        for fid in list(face_state.keys()):
            if fid not in seen_fids:
                del face_state[fid]

        # Speaker selection with hysteresis.
        if active_fid is None or active_fid not in seen_fids:
            # No active speaker yet or they left frame — pick highest scoring.
            active_fid = max(faces, key=lambda f: f["score"])["fid"]
            challenger_fid = None
            challenger_streak = 0
        else:
            best = max(faces, key=lambda f: f["score"])
            if best["fid"] != active_fid:
                active_score = next((f["score"] for f in faces if f["fid"] == active_fid), 0)
                # Only challenge if clearly better (20% margin) to avoid noise flips.
                if best["score"] > active_score * 1.20:
                    if challenger_fid == best["fid"]:
                        challenger_streak += 1
                    else:
                        challenger_fid = best["fid"]
                        challenger_streak = 1
                    if challenger_streak >= _SWITCH_FRAMES:
                        active_fid = challenger_fid
                        challenger_fid = None
                        challenger_streak = 0
                else:
                    challenger_fid = None
                    challenger_streak = 0
            else:
                challenger_fid = None
                challenger_streak = 0

        active_face = next((f for f in faces if f["fid"] == active_fid), faces[0])
        raw_track.append({
            "t": float(round(t, 3)),
            "cx": float(round(active_face["cx"], 4)),
            "cy": float(round(active_face["cy"], 4)),
            "fh": float(round(active_face["fh"], 4)),
        })
        frame_records.append({
            "t": float(round(t, 3)),
            "faces": [
                {
                    "fid": f["fid"],
                    "cx": float(f["cx"]),
                    "cy": float(f["cy"]),
                    "fh": float(f["fh"]),
                    "lip": float(f["lip"]),
                }
                for f in faces
            ],
        })

        if progress_callback and idx % 20 == 0:
            progress_callback(int(80 * idx / max(1, len(frame_files))))

    # Cleanup frames
    for fpath in frame_files:
        try:
            fpath.unlink()
        except OSError:
            pass
    try:
        frames_dir.rmdir()
    except OSError:
        pass

    # Diarization-guided remap: assign each speaker label to a face_id by
    # accumulating lip-motion within their turns, then rebuild track to follow
    # the diarized active speaker at each timestamp.
    try:
        from services.diarizer import load_diarization
        turns = load_diarization(job_id) or []
    except Exception:
        turns = []

    if turns and frame_records:
        speaker_fid = _map_speakers_to_faces(frame_records, turns)
        if speaker_fid:
            diar_track = _build_diarized_track(frame_records, turns, speaker_fid, raw_track)
            if diar_track:
                raw_track = diar_track

    smoothed = _smooth_track(raw_track)
    _save_track(job_id, smoothed)

    if progress_callback:
        progress_callback(100)

    return smoothed


def _extract_lip_crop(img, fx: float, fy: float, fw: float, fh: float):
    """Return grayscale 32x16 lip-region crop, or None on bad coords."""
    import cv2
    h, w = img.shape[:2]
    x0 = int((fx + fw * 0.2) * w)
    x1 = int((fx + fw * 0.8) * w)
    y0 = int((fy + fh * 0.65) * h)
    y1 = int((fy + fh * 0.95) * h)
    if x1 <= x0 or y1 <= y0:
        return None
    gray = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (32, 16))


def _lip_motion_score_stable(img, fx: float, fy: float, fw: float, fh: float, prev_crop) -> float:
    """Lip motion vs prev frame for the *same* stable face ID."""
    import cv2
    crop = _extract_lip_crop(img, fx, fy, fw, fh)
    if crop is None or prev_crop is None or prev_crop.shape != crop.shape:
        return 0.0
    diff = cv2.absdiff(crop, prev_crop)
    return float(diff.mean()) / 255.0


def _map_speakers_to_faces(frame_records: list, turns: list) -> dict:
    """For each diarized speaker, pick the face_id with highest cumulative
    (lip_motion * area) score during that speaker's turns.

    Returns {speaker_label: fid}.
    """
    # speaker -> fid -> score
    tally: dict = {}
    fi = 0
    for turn in turns:
        s, e, spk = turn["start"], turn["end"], turn["speaker"]
        while fi < len(frame_records) and frame_records[fi]["t"] < s:
            fi += 1
        j = fi
        while j < len(frame_records) and frame_records[j]["t"] <= e:
            for f in frame_records[j]["faces"]:
                score = (f["lip"] + 0.05) * (f["fh"] ** 2)
                tally.setdefault(spk, {}).setdefault(f["fid"], 0.0)
                tally[spk][f["fid"]] += score
            j += 1

    mapping: dict = {}
    used_fids: set = set()
    # Assign greedily by strongest speaker-fid affinity first to avoid two
    # speakers claiming the same face when one is clearly more dominant.
    pairs = [
        (spk, fid, sc)
        for spk, by_fid in tally.items()
        for fid, sc in by_fid.items()
    ]
    pairs.sort(key=lambda x: x[2], reverse=True)
    for spk, fid, _ in pairs:
        if spk in mapping or fid in used_fids:
            continue
        mapping[spk] = fid
        used_fids.add(fid)
    return mapping


def _build_diarized_track(frame_records: list, turns: list, speaker_fid: dict,
                           fallback_track: List[dict]) -> List[dict]:
    """At each sampled timestamp, look up active speaker → mapped face_id →
    that face's position. Fallback to highest-score face if unmapped/absent.
    """
    # Pair frame_records with fallback by index (both built in same loop order)
    # to avoid float-key fragility.
    out = []
    ti = 0
    for ri, rec in enumerate(frame_records):
        t = rec["t"]
        fb = fallback_track[ri] if ri < len(fallback_track) else None
        while ti < len(turns) and turns[ti]["end"] < t:
            ti += 1
        active_spk = None
        if ti < len(turns) and turns[ti]["start"] <= t <= turns[ti]["end"]:
            active_spk = turns[ti]["speaker"]
        target_fid = speaker_fid.get(active_spk) if active_spk else None
        face = None
        if target_fid is not None:
            face = next((f for f in rec["faces"] if f["fid"] == target_fid), None)
        if face is None:
            if fb is None:
                continue
            out.append(fb)
            continue
        out.append({
            "t": t,
            "cx": float(round(face["cx"], 4)),
            "cy": float(round(face["cy"], 4)),
            "fh": float(round(face["fh"], 4)),
        })
    return out


def _smooth_track(track: List[dict]) -> List[dict]:
    if not track:
        return []
    out = []
    cx, cy, fh = track[0]["cx"], track[0]["cy"], track[0].get("fh", 0.2)
    # Held position emitted to track; only updated when smoothed value drifts
    # outside the deadband. Suppresses sub-pixel wobble from Haar bbox jitter.
    held_cx, held_cy = cx, cy
    for pt in track:
        cx = _EMA_ALPHA * pt["cx"] + (1 - _EMA_ALPHA) * cx
        cy = _EMA_ALPHA * pt["cy"] + (1 - _EMA_ALPHA) * cy
        fh = _EMA_ALPHA * pt.get("fh", fh) + (1 - _EMA_ALPHA) * fh
        if abs(cx - held_cx) > _DEADBAND:
            held_cx = cx
        if abs(cy - held_cy) > _DEADBAND:
            held_cy = cy
        out.append({"t": float(pt["t"]), "cx": float(round(held_cx, 4)), "cy": float(round(held_cy, 4)), "fh": float(round(fh, 4))})
    return out


def _save_track(job_id: str, track: List[dict]):
    p = focus_track_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(track, f)


def _probe_video(path: str):
    """Return (width, height, duration_seconds)."""
    try:
        result = subprocess.run(
            [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration",
             "-of", "json", path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        s = data["streams"][0]
        return int(s["width"]), int(s["height"]), float(s.get("duration", 0))
    except Exception:
        return 0, 0, 0


def slice_track(track: List[dict], start: float, end: float) -> List[dict]:
    """Extract focus points within [start, end], shifted to clip-local time."""
    out = []
    for pt in track:
        if start <= pt["t"] <= end:
            out.append({
                "t": round(pt["t"] - start, 3),
                "cx": pt["cx"],
                "cy": pt["cy"],
                "fh": pt.get("fh", 0.2),
            })
    return out


def build_crop_expression(clip_track: List[dict], src_w: int, src_h: int, target_w: int, target_h: int) -> Optional[str]:
    """Build ffmpeg crop expression — TikTok-style zoom on speaker.

    Uses static zoom (mean face height across clip) so frame doesn't pump in/out.
    Pans x/y to track speaker. Face positioned in upper third of crop.
    Returns {"cw", "ch", "x", "y"} or None if track empty.
    """
    if not clip_track:
        return None

    # Compute mean face height (normalized 0..1 of source height) for static zoom level.
    fh_values = [p.get("fh", 0.2) for p in clip_track if p.get("fh", 0) > 0]
    if not fh_values:
        return None
    fh_mean = sum(fh_values) / len(fh_values)

    # Crop window height = ~4.5x face height (head + shoulders + chest fits, face ~22%).
    # Clamp so we don't crop bigger than source or zoom-in past meaningful detail.
    ZOOM_FACTOR = 4.5
    crop_h_norm = max(0.45, min(0.95, fh_mean * ZOOM_FACTOR))
    crop_h = int(src_h * crop_h_norm)
    crop_w = int(crop_h * target_w / target_h)

    # If width exceeds source, shrink crop_h proportionally.
    if crop_w > src_w:
        crop_w = src_w
        crop_h = int(src_w * target_h / target_w)

    # Force even dims (libx264 requirement).
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2

    # Cap keyframes for ffmpeg expression reliability. Pick points where the
    # crop position actually changes — avoids panning between near-identical
    # neighbors that produces visible side-to-side wobble.
    MAX_KEYS = 8
    MOVE_THRESHOLD = 0.02  # normalized; ignore sub-2% shifts
    pts = [clip_track[0]]
    last = clip_track[0]
    for pt in clip_track[1:]:
        if (abs(pt["cx"] - last["cx"]) > MOVE_THRESHOLD or
                abs(pt["cy"] - last["cy"]) > MOVE_THRESHOLD):
            pts.append(pt)
            last = pt
    if pts[-1]["t"] != clip_track[-1]["t"]:
        pts.append(clip_track[-1])
    if len(pts) > MAX_KEYS:
        idxs = [int(i * (len(pts) - 1) / (MAX_KEYS - 1)) for i in range(MAX_KEYS)]
        pts = [pts[i] for i in idxs]

    cx_expr = _piecewise_expr(pts, "cx")
    cy_expr = _piecewise_expr(pts, "cy")

    # Face composition: place face_y at ~1/3 from top of crop window.
    UPPER_THIRD = 0.33
    x_expr = f"max(0,min({src_w - crop_w},({cx_expr})*{src_w}-{crop_w / 2}))"
    y_expr = f"max(0,min({src_h - crop_h},({cy_expr})*{src_h}-{crop_h * UPPER_THIRD}))"

    # ffmpeg filter parser splits on top-level ',' regardless of paren depth.
    # Escape every comma inside expressions so they reach the expression evaluator intact.
    x_expr = x_expr.replace(",", "\\,")
    y_expr = y_expr.replace(",", "\\,")

    return {"cw": crop_w, "ch": crop_h, "x": x_expr, "y": y_expr}


# ── Face-focused mode (single dominant face, TikTok-style framing) ──────────


class OneEuroFilter:
    """Adaptive low-pass filter — low cutoff when still (kills jitter),
    high cutoff when moving fast (kills lag). Reference: Casiez et al. 2012.
    """

    def __init__(self, freq: float, min_cutoff: float = 1.0,
                 beta: float = 0.05, d_cutoff: float = 1.0):
        self.freq = freq
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x_prev: Optional[float] = None
        self._dx_prev: float = 0.0
        self._t_prev: Optional[float] = None

    def reset(self):
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x: float, t: float) -> float:
        if self._x_prev is None or self._t_prev is None:
            self._x_prev = x
            self._t_prev = t
            return x
        dt = max(1e-3, t - self._t_prev)
        dx = (x - self._x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1 - a_d) * self._dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1 - a) * self._x_prev
        self._x_prev = x_hat
        self._dx_prev = dx_hat
        self._t_prev = t
        return x_hat


def _detect_scenes(source: str, duration: float) -> List[float]:
    """Return list of scene-cut timestamps (seconds, sorted, excluding 0 and end).

    Uses PySceneDetect ContentDetector. Returns [] if unavailable or single scene.
    """
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import ContentDetector
    except ImportError:
        return []
    try:
        video = open_video(source)
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=27.0, min_scene_len=15))
        sm.detect_scenes(video, show_progress=False)
        scenes = sm.get_scene_list()
        cuts = []
        for scene in scenes[1:]:  # skip first scene start (== 0)
            cuts.append(float(scene[0].get_seconds()))
        return [c for c in cuts if 0.5 < c < duration - 0.5]
    except Exception:
        return []


def compute_face_track(job_id: str, progress_callback=None) -> List[dict]:
    """Sample at 10 FPS, MediaPipe detect biggest face per frame, smooth w/
    One Euro, reset at scene cuts. Persist to face_track.json.

    Track entry: {t, cx, cy, fh, scene}. cx/cy = eye-midpoint (or bbox center
    fallback) normalized to source dims. fh = face bbox height normalized.
    scene = integer scene index for cut-aware rendering.
    """
    existing = load_face_track(job_id)
    if existing:
        if progress_callback:
            progress_callback(100)
        return existing

    storage = os.getenv("STORAGE_PATH", "./storage")
    source = Path(storage) / "jobs" / job_id / "original.mp4"
    if not source.exists():
        return []

    try:
        import cv2
    except ImportError:
        _save_face_track(job_id, [])
        return []

    src_w, src_h, duration = _probe_video(str(source))
    if duration <= 0:
        _save_face_track(job_id, [])
        return []

    # Scene cuts (best-effort)
    cuts = _detect_scenes(str(source), duration)

    # Extract dense sampled frames at lower res for speed.
    frames_dir = Path(storage) / "jobs" / job_id / "_face_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(frames_dir / "f_%06d.jpg")
    try:
        subprocess.run(
            [ffmpeg_path(), "-y", "-i", str(source),
             "-vf", f"fps={_FACE_SAMPLE_FPS},scale=640:-2",
             "-q:v", "3", pattern],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        _save_face_track(job_id, [])
        return []

    frame_files = sorted(frames_dir.glob("f_*.jpg"))
    if not frame_files:
        _save_face_track(job_id, [])
        return []

    # MediaPipe primary; Haar fallback if mediapipe missing.
    mp_detector = None
    try:
        import mediapipe as mp
        mp_detector = mp.solutions.face_detection.FaceDetection(
            model_selection=1, min_detection_confidence=_FACE_MIN_CONFIDENCE
        )
    except Exception:
        mp_detector = None

    if mp_detector is None:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        haar = cv2.CascadeClassifier(cascade_path)
    else:
        haar = None

    raw = []
    last_cx, last_cy, last_fh = 0.5, 0.4, 0.22
    for idx, fpath in enumerate(frame_files):
        t = idx / _FACE_SAMPLE_FPS
        img = cv2.imread(str(fpath))
        if img is None:
            continue
        h, w = img.shape[:2]
        cx, cy, fh = None, None, None

        if mp_detector is not None:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            res = mp_detector.process(rgb)
            if res.detections:
                # Pick highest confidence × area (most prominent face).
                best, best_score = None, -1.0
                for det in res.detections:
                    bb = det.location_data.relative_bounding_box
                    if bb.width <= 0 or bb.height <= 0:
                        continue
                    conf = det.score[0] if det.score else 0.0
                    score = conf * bb.width * bb.height
                    if score > best_score:
                        best_score = score
                        best = det
                if best is not None:
                    bb = best.location_data.relative_bounding_box
                    # Eye-midpoint anchor (keypoints 0,1 = right/left eye).
                    kps = best.location_data.relative_keypoints
                    if kps and len(kps) >= 2:
                        cx = (kps[0].x + kps[1].x) / 2.0
                        cy = (kps[0].y + kps[1].y) / 2.0
                    else:
                        cx = bb.xmin + bb.width / 2.0
                        cy = bb.ymin + bb.height / 2.0
                    fh = bb.height
        else:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            rects = haar.detectMultiScale(gray, scaleFactor=1.15,
                                          minNeighbors=4, minSize=(30, 30))
            if len(rects) > 0:
                rx, ry, rw, rh = max(rects, key=lambda r: r[2] * r[3])
                cx = (rx + rw / 2) / w
                cy = (ry + rh / 2) / h
                fh = rh / h

        if cx is None:
            # Hold last position; mark missing so smoother won't pan to it.
            cx, cy, fh = last_cx, last_cy, last_fh
            missing = True
        else:
            cx = max(0.0, min(1.0, cx))
            cy = max(0.0, min(1.0, cy))
            fh = max(0.02, min(0.95, fh))
            last_cx, last_cy, last_fh = cx, cy, fh
            missing = False

        raw.append({"t": float(round(t, 3)),
                    "cx": float(cx), "cy": float(cy), "fh": float(fh),
                    "missing": missing})

        if progress_callback and idx % 30 == 0:
            progress_callback(int(80 * idx / max(1, len(frame_files))))

    if mp_detector is not None:
        try:
            mp_detector.close()
        except Exception:
            pass

    # Cleanup frames
    for fpath in frame_files:
        try:
            fpath.unlink()
        except OSError:
            pass
    try:
        frames_dir.rmdir()
    except OSError:
        pass

    smoothed = _smooth_face_track(raw, cuts)
    _save_face_track(job_id, smoothed)
    if progress_callback:
        progress_callback(100)
    return smoothed


def _smooth_face_track(raw: List[dict], cuts: List[float]) -> List[dict]:
    """One Euro Filter per axis. Reset filter state at every scene cut.
    Assigns scene_id per sample.
    """
    if not raw:
        return []
    fx = OneEuroFilter(freq=_FACE_SAMPLE_FPS, min_cutoff=1.0, beta=0.05)
    fy = OneEuroFilter(freq=_FACE_SAMPLE_FPS, min_cutoff=1.0, beta=0.05)
    fz = OneEuroFilter(freq=_FACE_SAMPLE_FPS, min_cutoff=0.5, beta=0.02)
    cuts_sorted = sorted(cuts)
    ci = 0
    scene = 0
    out = []
    for pt in raw:
        t = pt["t"]
        while ci < len(cuts_sorted) and t >= cuts_sorted[ci]:
            ci += 1
            scene += 1
            fx.reset()
            fy.reset()
            fz.reset()
        cx_s = fx(pt["cx"], t)
        cy_s = fy(pt["cy"], t)
        fh_s = fz(pt["fh"], t)
        out.append({
            "t": t,
            "cx": float(round(cx_s, 4)),
            "cy": float(round(cy_s, 4)),
            "fh": float(round(fh_s, 4)),
            "scene": scene,
        })
    return out


def _save_face_track(job_id: str, track: List[dict]):
    p = face_track_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(track, f)


def slice_face_track(track: List[dict], start: float, end: float) -> List[dict]:
    """Extract face-track points within [start, end], shifted to clip-local time.
    Re-numbers scene IDs from 0 within the slice.
    """
    out = []
    base_scene = None
    for pt in track:
        if start <= pt["t"] <= end:
            if base_scene is None:
                base_scene = pt.get("scene", 0)
            out.append({
                "t": round(pt["t"] - start, 3),
                "cx": pt["cx"],
                "cy": pt["cy"],
                "fh": pt.get("fh", 0.2),
                "scene": pt.get("scene", 0) - base_scene,
            })
    return out


def build_face_crop_expression(clip_track: List[dict], src_w: int, src_h: int,
                                target_w: int, target_h: int) -> Optional[dict]:
    """TikTok-style face crop. Adaptive zoom (never upscales source), face on
    upper third, ≥15% margin all sides, instantaneous jumps at scene cuts.
    """
    if not clip_track:
        return None
    fh_values = [p.get("fh", 0.2) for p in clip_track if p.get("fh", 0) > 0]
    if not fh_values:
        return None
    # 95th percentile face height → zoom for largest-face moment (so face never
    # exceeds the crop). Use a tighter 4.0× multiplier than speaker mode.
    fh_sorted = sorted(fh_values)
    fh_p95 = fh_sorted[int(0.95 * (len(fh_sorted) - 1))]
    ZOOM_FACTOR = 4.0
    crop_h_norm = max(0.5, min(0.95, fh_p95 * ZOOM_FACTOR))

    # Adaptive: ensure rendered crop_w >= target_w (1080) when source allows,
    # else open crop wider rather than upscaling.
    crop_h = int(src_h * crop_h_norm)
    crop_w = int(crop_h * target_w / target_h)
    if crop_w > src_w:
        crop_w = src_w
        crop_h = int(src_w * target_h / target_w)
    # If source > target_w but our crop_w < target_w, widen until ≥ target_w
    # (caps quality loss from upscale).
    min_crop_w = min(src_w, target_w)
    if crop_w < min_crop_w:
        crop_w = min_crop_w
        crop_h = int(crop_w * target_h / target_w)
        if crop_h > src_h:
            crop_h = src_h
            crop_w = int(crop_h * target_w / target_h)

    crop_w -= crop_w % 2
    crop_h -= crop_h % 2

    # Margin guarantee: ≥15% padding around the face within the crop.
    # Achieved by clamping pan so face bbox sits in central 70% of crop.
    MARGIN = 0.15

    # Build keyframes — keep all (10 FPS) but cap explosion via move threshold,
    # and split scenes by inserting a tiny-dt jump at each cut boundary.
    MOVE_THRESHOLD = 0.008
    MAX_KEYS = 64
    pts = [clip_track[0]]
    last = clip_track[0]
    for pt in clip_track[1:]:
        scene_change = pt.get("scene", 0) != last.get("scene", 0)
        moved = (abs(pt["cx"] - last["cx"]) > MOVE_THRESHOLD or
                 abs(pt["cy"] - last["cy"]) > MOVE_THRESHOLD)
        if scene_change:
            # Lock the previous position to the cut instant, then jump.
            pts.append({**last, "t": max(last["t"], pt["t"] - 0.001)})
            pts.append(pt)
            last = pt
        elif moved:
            pts.append(pt)
            last = pt
    if pts[-1]["t"] != clip_track[-1]["t"]:
        pts.append(clip_track[-1])
    if len(pts) > MAX_KEYS:
        idxs = [int(i * (len(pts) - 1) / (MAX_KEYS - 1)) for i in range(MAX_KEYS)]
        pts = [pts[i] for i in idxs]

    cx_expr = _piecewise_expr(pts, "cx")
    cy_expr = _piecewise_expr(pts, "cy")

    # Face anchor at upper third of crop window. Clamp pan to keep margin.
    UPPER_THIRD = 0.38
    # Effective x position formula: cx*src_w - crop_w/2, then clamp.
    # Margin clamp: face_center_x in crop should be in [MARGIN, 1-MARGIN] * crop_w.
    # cx_in_crop = cx*src_w - x  →  x ∈ [cx*src_w - (1-MARGIN)*crop_w, cx*src_w - MARGIN*crop_w]
    # Combined with source bounds [0, src_w - crop_w]:
    x_lo = f"max(0,({cx_expr})*{src_w}-{(1.0 - MARGIN) * crop_w})"
    x_hi = f"min({src_w - crop_w},({cx_expr})*{src_w}-{MARGIN * crop_w})"
    x_center = f"({cx_expr})*{src_w}-{crop_w / 2}"
    x_expr = f"max({x_lo},min({x_hi},{x_center}))"

    y_lo = f"max(0,({cy_expr})*{src_h}-{(1.0 - MARGIN) * crop_h})"
    y_hi = f"min({src_h - crop_h},({cy_expr})*{src_h}-{MARGIN * crop_h})"
    y_center = f"({cy_expr})*{src_h}-{crop_h * UPPER_THIRD}"
    y_expr = f"max({y_lo},min({y_hi},{y_center}))"

    x_expr = x_expr.replace(",", "\\,")
    y_expr = y_expr.replace(",", "\\,")
    return {"cw": crop_w, "ch": crop_h, "x": x_expr, "y": y_expr}


def _piecewise_expr(pts: List[dict], key: str) -> str:
    """Linear interpolation expression for ffmpeg eval."""
    if len(pts) == 1:
        return f"{pts[0][key]}"
    expr = f"{pts[-1][key]}"
    for i in range(len(pts) - 1, 0, -1):
        t0 = pts[i - 1]["t"]
        t1 = pts[i]["t"]
        v0 = pts[i - 1][key]
        v1 = pts[i][key]
        dt = max(0.001, t1 - t0)
        slope = (v1 - v0) / dt
        seg = f"({v0}+({slope})*(t-{t0}))"
        expr = f"if(lt(t,{t1}),{seg},{expr})"
    return expr
