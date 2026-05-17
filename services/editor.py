"""Unified single-pass clip renderer.

The previous pipeline encoded each clip three times sequentially:
    raw extract  →  aspect transform  →  caption burn
This module collapses those into ONE ffmpeg invocation per clip via
filter_complex chains, and selects an H.264 hardware encoder when one is
available (VideoToolbox / NVENC / QSV / AMF) with libx264 as fallback.

Public API:
    render_and_caption_clip(...)  — single-pass producer of final .mp4
    burn_captions(...)            — legacy wrapper kept for export path

Returns dicts with explicit "error" keys so callers can mark DB rows
"failed" instead of swallowing exceptions.
"""
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from services.media_tools import (
    ffmpeg_path,
    ffprobe_path,
    encoder_video_opts,
    encoder_audio_opts,
)


def get_clips_dir(job_id: str) -> Path:
    storage = os.getenv("STORAGE_PATH", "./storage")
    p = Path(storage) / "jobs" / job_id / "clips"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Source dimensions cache helper ──────────────────────────────────────────

def probe_source_dims(source_path: str) -> tuple[int, int]:
    """Return (width, height) of a video file via ffprobe."""
    result = subprocess.run(
        [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", source_path],
        capture_output=True, text=True, check=True, timeout=30,
    )
    w, h = result.stdout.strip().split("x")
    return int(w), int(h)


# ── Filter chain builder ────────────────────────────────────────────────────

def _build_video_filter(
    aspect_ratio: str,
    focus_mode: str,
    focus_crop_expr: Optional[dict],
    captions_ass_path: Optional[str],
) -> str:
    """Build a single -filter_complex string ending at [out]."""
    # Stage 1: aspect transform → [composed]
    if aspect_ratio == "9:16":
        if focus_mode == "face" and focus_crop_expr:
            chain = (
                f"[0:v]crop={focus_crop_expr['cw']}:{focus_crop_expr['ch']}:"
                f"{focus_crop_expr['x']}:{focus_crop_expr['y']},"
                f"scale=1080:1920:flags=lanczos[composed]"
            )
        elif focus_mode == "speaker" and focus_crop_expr:
            chain = (
                f"[0:v]crop={focus_crop_expr['cw']}:{focus_crop_expr['ch']}:"
                f"{focus_crop_expr['x']}:{focus_crop_expr['y']},"
                f"scale=1080:1920[composed]"
            )
        elif focus_mode == "center":
            chain = (
                "[0:v]scale=iw:ih,"
                "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
                "scale=1080:1920[composed]"
            )
        else:
            # Default 9:16: blurred background + scaled foreground overlay
            chain = (
                "[0:v]split=2[bg][fg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,boxblur=10:2[blurred];"
                "[fg]scale=1080:-2[scaled];"
                "[blurred][scaled]overlay=(W-w)/2:(H-h)/2[composed]"
            )
    elif aspect_ratio == "1:1":
        chain = (
            "[0:v]scale=1080:1080:force_original_aspect_ratio=increase,"
            "crop=1080:1080[composed]"
        )
    else:
        # Native / 16:9: just normalize to 1080p height
        chain = "[0:v]scale=-2:1080[composed]"

    # Stage 2: caption burn → [out]
    if captions_ass_path:
        escaped = captions_ass_path.replace("\\", "\\\\").replace(":", "\\:")
        chain += f";[composed]ass={escaped}[out]"
    else:
        # Rename [composed] → [out] so the -map target is consistent
        chain = chain[::-1].replace("[composed]"[::-1], "[out]"[::-1], 1)[::-1]

    return chain


# ── ASS subtitle generation (unchanged caption layer) ───────────────────────

_COMPLEX_SCRIPT_LANGS = {"hi", "mr", "ne", "sa", "ta", "te", "kn", "ml",
                         "gu", "pa", "bn", "ur"}


def _extract_words_in_range(segments: list, start: float, end: float) -> list:
    words = []
    for seg in segments:
        for w in seg.get("words", []):
            ws = w.get("start", 0)
            we = w.get("end", 0)
            if we > start and ws < end:
                words.append({
                    "word": w["word"],
                    "start": max(0.0, ws - start),
                    "end": min(end - start, we - start),
                })
    return words


def _extract_sentences_in_range(segments: list, start: float, end: float) -> list:
    words = []
    for seg in segments:
        ss = seg.get("start", 0)
        se = seg.get("end", 0)
        text = seg.get("text", "").strip()
        if not text or se < start or ss > end:
            continue
        words.append({
            "word": text,
            "start": max(0.0, ss - start),
            "end": max(0.0, se - start),
        })
    return words


def _pick_font(language: str) -> str:
    # ASS Fontname is a single font name, not a fallback list — libass treats
    # commas literally. Pick one name that is actually installed in the
    # container (fonts-noto*, fonts-indic via Dockerfile). Fontconfig in libass
    # will still substitute close matches if exact name missing.
    if language in {"hi", "mr", "ne", "sa"}:
        return "Noto Sans Devanagari"
    if language == "ta":
        return "Noto Sans Tamil"
    if language == "te":
        return "Noto Sans Telugu"
    if language == "bn":
        return "Noto Sans Bengali"
    if language == "gu":
        return "Noto Sans Gujarati"
    if language == "pa":
        return "Noto Sans Gurmukhi"
    if language == "kn":
        return "Noto Sans Kannada"
    if language == "ml":
        return "Noto Sans Malayalam"
    return "Noto Sans"


def _format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _generate_ass(words: list, font: str, complex_script: bool = False,
                  style: str = "word_highlight") -> str:
    from services.caption_styles import get_style, COMPLEX_SAFE_ANIMATIONS

    cfg = get_style(style)
    animation = cfg["animation"]
    if complex_script and animation not in COMPLEX_SAFE_ANIMATIONS:
        animation = "sentence"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{cfg["font_size"]},{cfg["primary"]},&H000000FF,{cfg["outline_color"]},{cfg["back_color"]},{cfg["bold"]},0,0,0,100,100,0,0,{cfg["border_style"]},{cfg["outline"]},{cfg["shadow"]},{cfg["alignment"]},40,40,{cfg["margin_v"]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    groups = _group_words(words, max_words=4)
    events = []
    for group in groups:
        if animation == "sentence":
            events.extend(_anim_sentence(group))
        elif animation == "per_word":
            events.extend(_anim_per_word(group, cfg))
        elif animation == "karaoke_pop":
            events.extend(_anim_karaoke_pop(group, cfg))
        elif animation == "typewriter":
            events.extend(_anim_typewriter(group, cfg))
        elif animation == "gradient_pop":
            events.extend(_anim_gradient_pop(group, cfg))
        else:
            events.extend(_anim_sentence(group))
    return header + "\n".join(events) + "\n"


def _anim_sentence(group: list) -> list:
    line_start = _format_ass_time(group[0]["start"])
    line_end = _format_ass_time(group[-1]["end"])
    text = " ".join(w["word"].strip() for w in group)
    return [f"Dialogue: 0,{line_start},{line_end},Default,,0,0,0,,{text}"]


def _anim_per_word(group: list, cfg: dict) -> list:
    events = []
    hl, dim, primary = cfg["highlight"], cfg["dim"], cfg["primary"]
    for active_idx, active_word in enumerate(group):
        seg_start = _format_ass_time(active_word["start"])
        seg_end = _format_ass_time(active_word["end"])
        parts = []
        for i, w in enumerate(group):
            text = w["word"].strip()
            if i == active_idx:
                parts.append(f"{{\\1c{hl}}}{text}{{\\1c{primary}}}")
            else:
                parts.append(f"{{\\1c{dim}}}{text}{{\\1c{primary}}}")
        events.append(f"Dialogue: 0,{seg_start},{seg_end},Default,,0,0,0,,{' '.join(parts)}")
    return events


def _anim_karaoke_pop(group: list, cfg: dict) -> list:
    events = []
    hl, primary, dim = cfg["highlight"], cfg["primary"], cfg["dim"]
    for active_idx, active_word in enumerate(group):
        seg_start = _format_ass_time(active_word["start"])
        seg_end = _format_ass_time(active_word["end"])
        dur_ms = max(50, int((active_word["end"] - active_word["start"]) * 1000))
        pop_ms = min(150, dur_ms // 3)
        parts = []
        for i, w in enumerate(group):
            text = w["word"].strip()
            if i == active_idx:
                parts.append(
                    f"{{\\1c{hl}\\t(0,{pop_ms},\\fscx125\\fscy125)\\t({pop_ms},{dur_ms},\\fscx100\\fscy100)}}{text}{{\\1c{primary}\\fscx100\\fscy100}}"
                )
            elif i < active_idx:
                parts.append(f"{{\\1c{primary}}}{text}")
            else:
                parts.append(f"{{\\1c{dim}}}{text}{{\\1c{primary}}}")
        events.append(f"Dialogue: 0,{seg_start},{seg_end},Default,,0,0,0,,{' '.join(parts)}")
    return events


def _anim_typewriter(group: list, cfg: dict) -> list:
    events = []
    group_end = _format_ass_time(group[-1]["end"])
    for active_idx, active_word in enumerate(group):
        seg_start = _format_ass_time(active_word["start"])
        next_start = _format_ass_time(group[active_idx + 1]["start"]) if active_idx + 1 < len(group) else group_end
        visible_words = " ".join(w["word"].strip() for w in group[: active_idx + 1])
        events.append(f"Dialogue: 0,{seg_start},{next_start},Default,,0,0,0,,{visible_words}")
    return events


def _anim_gradient_pop(group: list, cfg: dict) -> list:
    events = []
    palette = cfg.get("palette", [cfg["highlight"]])
    primary, dim = cfg["primary"], cfg["dim"]
    for active_idx, active_word in enumerate(group):
        seg_start = _format_ass_time(active_word["start"])
        seg_end = _format_ass_time(active_word["end"])
        color = palette[active_idx % len(palette)]
        parts = []
        for i, w in enumerate(group):
            text = w["word"].strip()
            if i == active_idx:
                parts.append(f"{{\\1c{color}\\t(0,200,\\fscx115\\fscy115)\\t(200,400,\\fscx100\\fscy100)}}{text}{{\\1c{primary}\\fscx100\\fscy100}}")
            else:
                parts.append(f"{{\\1c{dim}}}{text}{{\\1c{primary}}}")
        events.append(f"Dialogue: 0,{seg_start},{seg_end},Default,,0,0,0,,{' '.join(parts)}")
    return events


def _group_words(words: list, max_words: int = 4) -> list:
    groups, current = [], []
    for w in words:
        current.append(w)
        if len(current) >= max_words:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


# ── Stream-copy fast path ───────────────────────────────────────────────────

def _stream_copy_cut(source: str, start: float, duration: float,
                     output: str) -> tuple[int, str]:
    """Cut [start, start+duration] from source via `-c copy`. No re-encode.

    Uses -ss before -i for keyframe-aligned input seek (fast). Caller decides
    whether stream copy is appropriate (no filter, no caption burn).
    """
    cmd = [
        ffmpeg_path(), "-y",
        "-ss", str(start),
        "-i", source,
        "-t", str(duration),
        "-c", "copy",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        output,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return 0, ""
    except subprocess.TimeoutExpired:
        return -1, "stream copy timeout"
    except subprocess.CalledProcessError as e:
        return e.returncode, (e.stderr or b"").decode("utf-8", errors="ignore")[-500:]


# ── Single-pass render (the heart) ──────────────────────────────────────────

def render_and_caption_clip(
    job_id: str,
    clip: dict,
    aspect_ratio: str = "9:16",
    focus_mode: str = "none",
    caption_style: str = "word_highlight",
    include_captions: bool = True,
    transcript: Optional[dict] = None,
    source_dims: Optional[tuple] = None,
    profile: str = "preview",
) -> dict:
    """One ffmpeg pass: cut → aspect transform → caption burn.

    Returns: {final_clip_path, error}. error is None on success, str on failure.
    Does NOT raise — caller decides how to record the failure.
    """
    storage = os.getenv("STORAGE_PATH", "./storage")
    source = str(Path(storage) / "jobs" / job_id / "original.mp4")
    if not os.path.exists(source):
        return {"final_clip_path": None, "error": f"source not found: {source}"}

    clips_dir = get_clips_dir(job_id)
    rank = clip.get("rank", 1)
    final_path = str(clips_dir / f"clip_{rank:03d}_final.mp4")

    start = clip.get("user_start_seconds") or clip["start_seconds"]
    end = clip.get("user_end_seconds") or clip["end_seconds"]
    if end <= start:
        return {"final_clip_path": None, "error": f"invalid range: {start}-{end}"}

    # NOTE: removed stream-copy fast path. -ss before -i jumps to the
    # nearest preceding keyframe, which often produces an mp4 that opens
    # mid-GOP — file size looks fine but the video is unplayable until
    # the next keyframe. Re-encoding (below) is the safe path; the
    # `effective_profile` switch later still makes 16:9 fast.

    # Probe source dims if not supplied (Phase E2: pipeline caches and passes)
    src_w = src_h = None
    if source_dims:
        src_w, src_h = source_dims
    elif focus_mode in ("speaker", "face"):
        try:
            src_w, src_h = probe_source_dims(source)
        except Exception as exc:
            return {"final_clip_path": None, "error": f"probe failed: {exc}"}

    # Speaker / face focus: compute crop expression from track if available
    focus_crop_expr = None
    if focus_mode == "face" and src_w and src_h:
        try:
            from services.speaker_focus import (
                load_face_track, slice_face_track, build_face_crop_expression,
            )
            track = load_face_track(job_id)
            if track:
                clip_track = slice_face_track(track, start, end)
                if clip_track:
                    focus_crop_expr = build_face_crop_expression(
                        clip_track, src_w, src_h, 9, 16
                    )
        except Exception:
            focus_crop_expr = None
        if not focus_crop_expr:
            focus_mode = "none"
    elif focus_mode == "speaker" and src_w and src_h:
        try:
            from services.speaker_focus import (
                load_focus_track, slice_track, build_crop_expression,
            )
            track = load_focus_track(job_id)
            if track:
                clip_track = slice_track(track, start, end)
                if clip_track:
                    focus_crop_expr = build_crop_expression(
                        clip_track, src_w, src_h, 9, 16
                    )
        except Exception:
            # Non-fatal: silently fall back to default 9:16 (blur+overlay)
            focus_crop_expr = None
        if not focus_crop_expr:
            focus_mode = "none"

    # Caption generation (writes .ass file alongside the clip)
    captions_ass_path = None
    if include_captions and caption_style != "none" and transcript:
        clip_words = _extract_words_in_range(
            transcript.get("segments", []), start, end
        )
        if not clip_words:
            clip_words = _extract_sentences_in_range(
                transcript.get("segments", []), start, end
            )
        if clip_words:
            language = transcript.get("language", "en")
            font = _pick_font(language)
            complex_script = language in _COMPLEX_SCRIPT_LANGS
            ass_path = str(clips_dir / f"clip_{rank:03d}.ass")
            ass_content = _generate_ass(
                clip_words, font, complex_script=complex_script, style=caption_style
            )
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(ass_content)
            captions_ass_path = ass_path

    filter_complex = _build_video_filter(
        aspect_ratio, focus_mode, focus_crop_expr, captions_ass_path
    )

    # Use libass-enabled ffmpeg if captions are baked in
    ff_bin = ffmpeg_path(require_libass=bool(captions_ass_path))

    def _run(video_opts: list[str]) -> tuple[int, str]:
        cmd = [
            ff_bin, "-y",
            "-ss", str(start),
            "-i", source,
            "-t", str(end - start),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?",
            *video_opts,
            *encoder_audio_opts(),
            final_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
            return 0, ""
        except subprocess.TimeoutExpired:
            return -1, "render timeout (>10min)"
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode("utf-8", errors="ignore")[-500:]
            return e.returncode, stderr

    # For native 16:9 export the only work is caption burn-in — the scale
    # filter is a no-op-ish 1080p normalize. Use a faster preset so a 60s
    # clip doesn't take 5 min on Railway shared CPU.
    effective_profile = profile
    if aspect_ratio in ("16:9", "native") and profile == "export":
        effective_profile = "preview"  # preset=ultrafast, CRF 26 — still clean

    rc, err = _run(encoder_video_opts(effective_profile))

    # Hardware encoder can list as available but fail at encode time
    # (NVENC on a CPU-only host, VideoToolbox under a sandboxed container, etc).
    # Fall back to libx264 once and remember the decision for this process.
    if rc != 0 and _is_hwaccel_failure(err):
        _disable_hwaccel_for_process()
        rc, err = _run(encoder_video_opts(effective_profile))

    if rc != 0:
        return {"final_clip_path": None, "error": f"ffmpeg failed: {err}"}

    if not Path(final_path).exists() or Path(final_path).stat().st_size < 1024:
        return {"final_clip_path": None, "error": "output missing or empty"}

    return {"final_clip_path": final_path, "error": None}


def _is_hwaccel_failure(stderr: str) -> bool:
    """Heuristic: did the failure come from a hardware encoder?

    Compile-time detection (ffmpeg -encoders) can list encoders that fail at
    runtime (NVENC on CPU-only host, etc). We match hw-specific tokens, not
    generic "invalid argument", to avoid falsely retrying real bugs.
    """
    s = (stderr or "").lower()
    return any(k in s for k in (
        "nvenc", "videotoolbox", "qsv", "h264_amf",
        "no nvidia", "cannot load nvcuda",
    ))


def _disable_hwaccel_for_process():
    """Force libx264 for remainder of process. Clears the lru_cache and pins ENCODER."""
    from services.media_tools import detect_hwaccel
    os.environ["ENCODER"] = "software"
    try:
        detect_hwaccel.cache_clear()
    except AttributeError:
        pass


# ── Legacy wrappers (export path still uses these) ──────────────────────────

def render_clip(job_id: str, clip: dict, aspect_ratio: str = "9:16",
                focus_mode: str = "none") -> dict:
    """Backward-compatible wrapper. Renders without captions; returns raw+final
    pointing at the same single-pass output file (no separate raw intermediate).
    """
    result = render_and_caption_clip(
        job_id, clip,
        aspect_ratio=aspect_ratio,
        focus_mode=focus_mode,
        include_captions=False,
        profile="export",
    )
    if result["error"]:
        raise RuntimeError(result["error"])
    return {"raw_clip_path": result["final_clip_path"],
            "final_clip_path": result["final_clip_path"]}


def burn_captions(clip: dict, transcript: dict, input_path: str,
                  output_path: str, style: str = "word_highlight"):
    """Legacy export-path caption burner. Kept for services/exporter.py.

    Note: the main pipeline no longer calls this — captions are baked into
    the single-pass render. This remains for the export endpoint which
    re-burns captions after composing the final export file.
    """
    start = clip.get("user_start_seconds") or clip["start_seconds"]
    end = clip.get("user_end_seconds") or clip["end_seconds"]

    clip_words = _extract_words_in_range(
        transcript.get("segments", []), start, end
    )
    if not clip_words:
        clip_words = _extract_sentences_in_range(
            transcript.get("segments", []), start, end
        )
        if not clip_words:
            shutil.copy(input_path, output_path)
            return

    language = transcript.get("language", "en")
    font = _pick_font(language)
    complex_script = language in _COMPLEX_SCRIPT_LANGS

    ass_path = input_path.replace(".mp4", ".ass")
    ass_content = _generate_ass(
        clip_words, font, complex_script=complex_script, style=style
    )
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_content)

    escaped = ass_path.replace("\\", "\\\\").replace(":", "\\:")
    subprocess.run(
        [ffmpeg_path(require_libass=True), "-y", "-i", input_path,
         "-vf", f"ass={escaped}",
         *encoder_video_opts("export"),
         *encoder_audio_opts(),
         output_path],
        check=True, capture_output=True, timeout=600,
    )
