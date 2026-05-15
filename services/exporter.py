import os
import shutil
from pathlib import Path

from services.editor import render_and_caption_clip
from services.transcriber import load_transcript


def export_clip(job_id: str, clip: dict, options: dict) -> str:
    aspect_ratio = options.get("aspect_ratio", "9:16")
    include_captions = options.get("include_captions", True)
    caption_style = options.get("caption_style", "word_highlight")
    focus_mode = options.get("focus_mode", "none")

    # Compute focus track lazily on first speaker-mode export.
    if focus_mode == "speaker":
        from services.speaker_focus import compute_focus_track
        try:
            compute_focus_track(job_id)
        except Exception:
            focus_mode = "none"

    storage = os.getenv("STORAGE_PATH", "./storage")
    clips_dir = Path(storage) / "jobs" / job_id / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    rank = clip.get("rank", 1)
    export_path = str(clips_dir / f"clip_{rank:03d}_export.mp4")

    try:
        transcript = load_transcript(job_id) if include_captions else None
    except FileNotFoundError:
        transcript = None

    # Single-pass: cut + aspect transform + captions all in one ffmpeg call
    result = render_and_caption_clip(
        job_id, clip,
        aspect_ratio=aspect_ratio,
        focus_mode=focus_mode,
        caption_style=caption_style,
        include_captions=include_captions and transcript is not None,
        transcript=transcript,
        profile="export",
    )
    if result["error"]:
        raise RuntimeError(f"Export render failed: {result['error']}")

    shutil.copy(result["final_clip_path"], export_path)

    if not Path(export_path).exists() or Path(export_path).stat().st_size < 1024:
        raise RuntimeError(f"Export produced empty or missing file: {export_path}")

    return export_path
