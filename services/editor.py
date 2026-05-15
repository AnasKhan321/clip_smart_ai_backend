import os
import shutil
import subprocess
from pathlib import Path
from services.media_tools import ffmpeg_path, ffprobe_path

# Production encode settings: CRF 18, yuv420p for broad compatibility
_VID_OPTS = ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-threads", "1"]
_AUD_OPTS = ["-c:a", "aac", "-b:a", "192k"]


def get_clips_dir(job_id: str) -> Path:
    storage = os.getenv("STORAGE_PATH", "./storage")
    p = Path(storage) / "jobs" / job_id / "clips"
    p.mkdir(parents=True, exist_ok=True)
    return p


def render_clip(job_id: str, clip: dict, aspect_ratio: str = "9:16", focus_mode: str = "none") -> dict:
    storage = os.getenv("STORAGE_PATH", "./storage")
    source = str(Path(storage) / "jobs" / job_id / "original.mp4")
    clips_dir = get_clips_dir(job_id)

    rank = clip.get("rank", 1)
    raw_path = str(clips_dir / f"clip_{rank:03d}_raw.mp4")
    final_path = str(clips_dir / f"clip_{rank:03d}_final.mp4")

    start = clip.get("user_start_seconds") or clip["start_seconds"]
    end = clip.get("user_end_seconds") or clip["end_seconds"]

    # Input seek for speed, re-encode for frame-accurate cut (no blank frames)
    subprocess.run(
        [ffmpeg_path(), "-y", "-ss", str(start), "-i", source, "-t", str(end - start)]
        + _VID_OPTS + _AUD_OPTS + [raw_path],
        check=True,
        capture_output=True,
    )

    if aspect_ratio == "9:16":
        if focus_mode == "speaker":
            ok = _apply_focused_vertical_crop(job_id, clip, raw_path, final_path, start, end)
            if not ok:
                _apply_vertical_crop(raw_path, final_path)
        elif focus_mode == "center":
            _apply_center_crop(raw_path, final_path, 9, 16)
        else:
            _apply_vertical_crop(raw_path, final_path)
    elif aspect_ratio == "1:1":
        _apply_square_crop(raw_path, final_path)
    else:
        shutil.copy(raw_path, final_path)

    return {"raw_clip_path": raw_path, "final_clip_path": final_path}


def _apply_vertical_crop(input_path: str, output_path: str):
    filter_complex = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=10:2[blurred];"
        "[fg]scale=1080:-2[scaled];"
        "[blurred][scaled]overlay=(W-w)/2:(H-h)/2[out]"
    )
    subprocess.run(
        [ffmpeg_path(), "-y", "-i", input_path,
         "-filter_complex", filter_complex,
         "-map", "[out]", "-map", "0:a?"]
        + _VID_OPTS + _AUD_OPTS + [output_path],
        check=True,
        capture_output=True,
    )


def _apply_center_crop(input_path: str, output_path: str, ar_w: int, ar_h: int):
    vf = (
        f"scale=iw:ih,crop='min(iw,ih*{ar_w}/{ar_h})':'min(ih,iw*{ar_h}/{ar_w})',"
        f"scale=1080:1920"
    )
    subprocess.run(
        [ffmpeg_path(), "-y", "-i", input_path, "-vf", vf]
        + _VID_OPTS + _AUD_OPTS + [output_path],
        check=True, capture_output=True,
    )


def _apply_focused_vertical_crop(job_id: str, clip: dict, input_path: str, output_path: str,
                                  src_start: float, src_end: float) -> bool:
    """Crop 9:16 around active speaker via precomputed focus_track. Returns False on failure."""
    from services.speaker_focus import load_focus_track, slice_track, build_crop_expression

    track = load_focus_track(job_id)
    if not track:
        return False

    clip_track = slice_track(track, src_start, src_end)
    if not clip_track:
        return False

    # Probe cut clip dims
    try:
        result = subprocess.run(
            [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", input_path],
            capture_output=True, text=True, check=True,
        )
        w, h = [int(x) for x in result.stdout.strip().split("x")]
    except Exception:
        return False

    expr = build_crop_expression(clip_track, w, h, 9, 16)
    if not expr:
        return False

    # Use filter_complex with explicit pad names because filter chain ',' separator
    # conflicts with commas inside crop expression (min/max/if).
    fc = (
        f"[0:v]crop={expr['cw']}:{expr['ch']}:{expr['x']}:{expr['y']}[cropped];"
        f"[cropped]scale=1080:1920[out]"
    )
    try:
        subprocess.run(
            [ffmpeg_path(), "-y", "-i", input_path,
             "-filter_complex", fc,
             "-map", "[out]", "-map", "0:a?"]
            + _VID_OPTS + _AUD_OPTS + [output_path],
            check=True, capture_output=True,
        )
        # Validate output: must exist and be > 1KB.
        if not Path(output_path).exists() or Path(output_path).stat().st_size < 1024:
            print(f"[focused_crop] output too small or missing: {output_path}", flush=True)
            return False
        return True
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="ignore")[-400:]
        print(f"[focused_crop] ffmpeg failed: {stderr}", flush=True)
        return False


def _apply_square_crop(input_path: str, output_path: str):
    vf = (
        "scale=1080:1080:force_original_aspect_ratio=increase,"
        "crop=1080:1080"
    )
    subprocess.run(
        [ffmpeg_path(), "-y", "-i", input_path, "-vf", vf]
        + _VID_OPTS + _AUD_OPTS + [output_path],
        check=True,
        capture_output=True,
    )


_COMPLEX_SCRIPT_LANGS = {"hi", "mr", "ne", "sa", "ta", "te", "kn", "ml", "gu", "pa", "bn", "ur"}


def burn_captions(clip: dict, transcript: dict, input_path: str, output_path: str, style: str = "word_highlight"):
    start = clip.get("user_start_seconds") or clip["start_seconds"]
    end = clip.get("user_end_seconds") or clip["end_seconds"]

    clip_words = _extract_words_in_range(transcript.get("segments", []), start, end)

    if not clip_words:
        clip_words = _extract_sentences_in_range(transcript.get("segments", []), start, end)
        if not clip_words:
            shutil.copy(input_path, output_path)
            return

    language = transcript.get("language", "en")
    font = _pick_font(language)
    complex_script = language in _COMPLEX_SCRIPT_LANGS

    ass_path = input_path.replace(".mp4", ".ass")
    ass_content = _generate_ass(clip_words, font, complex_script=complex_script, style=style)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_content)

    escaped = ass_path.replace("\\", "\\\\").replace(":", "\\:")
    subprocess.run(
        [ffmpeg_path(require_libass=True), "-y", "-i", input_path, "-vf", f"ass={escaped}"]
        + _VID_OPTS + _AUD_OPTS + [output_path],
        check=True,
        capture_output=True,
    )


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
    devanagari_langs = {"hi", "mr", "ne", "sa"}
    tamil_langs = {"ta"}
    telugu_langs = {"te"}
    if language in devanagari_langs:
        return "Kohinoor Devanagari"
    if language in tamil_langs:
        return "Tamil Sangam MN"
    if language in telugu_langs:
        return "Kohinoor Telugu"
    return "Montserrat"


def _format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _generate_ass(words: list, font: str, complex_script: bool = False, style: str = "word_highlight") -> str:
    from services.caption_styles import get_style, COMPLEX_SAFE_ANIMATIONS

    cfg = get_style(style)
    animation = cfg["animation"]

    # Force sentence animation for complex scripts — inline color tags break ligatures.
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
    hl = cfg["highlight"]
    dim = cfg["dim"]
    primary = cfg["primary"]
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
        events.append(
            f"Dialogue: 0,{seg_start},{seg_end},Default,,0,0,0,,{' '.join(parts)}"
        )
    return events


def _anim_karaoke_pop(group: list, cfg: dict) -> list:
    """Each word stays visible; active word scales up + highlight color."""
    events = []
    hl = cfg["highlight"]
    primary = cfg["primary"]
    dim = cfg["dim"]
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
        events.append(
            f"Dialogue: 0,{seg_start},{seg_end},Default,,0,0,0,,{' '.join(parts)}"
        )
    return events


def _anim_typewriter(group: list, cfg: dict) -> list:
    """Reveal word-by-word; each word appears at its start time, persists to group end."""
    events = []
    group_end = _format_ass_time(group[-1]["end"])
    for active_idx, active_word in enumerate(group):
        seg_start = _format_ass_time(active_word["start"])
        next_start = _format_ass_time(group[active_idx + 1]["start"]) if active_idx + 1 < len(group) else group_end
        visible_words = " ".join(w["word"].strip() for w in group[: active_idx + 1])
        events.append(
            f"Dialogue: 0,{seg_start},{next_start},Default,,0,0,0,,{visible_words}"
        )
    return events


def _anim_gradient_pop(group: list, cfg: dict) -> list:
    """Active word cycles through palette colors."""
    events = []
    palette = cfg.get("palette", [cfg["highlight"]])
    primary = cfg["primary"]
    dim = cfg["dim"]
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
        events.append(
            f"Dialogue: 0,{seg_start},{seg_end},Default,,0,0,0,,{' '.join(parts)}"
        )
    return events


def _group_words(words: list, max_words: int = 4) -> list:
    groups = []
    current = []
    for w in words:
        current.append(w)
        if len(current) >= max_words:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups
