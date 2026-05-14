"""Active-speaker face tracking. Computes per-job focus track JSON.

Track is a list of {t, cx, cy} normalized [0,1] over source video time.
Crop width/height derived at render time from target aspect ratio.
"""
import json
import os
import subprocess
from pathlib import Path
from typing import List, Optional
from services.media_tools import ffmpeg_path, ffprobe_path

_SAMPLE_FPS = 2.0
_EMA_ALPHA = 0.18


def focus_track_path(job_id: str) -> Path:
    storage = os.getenv("STORAGE_PATH", "./storage")
    return Path(storage) / "jobs" / job_id / "focus_track.json"


def load_focus_track(job_id: str) -> Optional[List[dict]]:
    p = focus_track_path(job_id)
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

    width, height, duration = _probe_video(str(source))
    if duration <= 0:
        _save_track(job_id, [])
        return []

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
                          "fw": cand["fw"]})

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


def _smooth_track(track: List[dict]) -> List[dict]:
    if not track:
        return []
    out = []
    cx, cy, fh = track[0]["cx"], track[0]["cy"], track[0].get("fh", 0.2)
    for pt in track:
        cx = _EMA_ALPHA * pt["cx"] + (1 - _EMA_ALPHA) * cx
        cy = _EMA_ALPHA * pt["cy"] + (1 - _EMA_ALPHA) * cy
        fh = _EMA_ALPHA * pt.get("fh", fh) + (1 - _EMA_ALPHA) * fh
        out.append({"t": float(pt["t"]), "cx": float(round(cx, 4)), "cy": float(round(cy, 4)), "fh": float(round(fh, 4))})
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

    # Cap keyframes for ffmpeg expression reliability.
    pts = clip_track
    MAX_KEYS = 8
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
