import os
import shutil
from pathlib import Path
from services.editor import render_clip, burn_captions
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

    render_result = render_clip(job_id, clip, aspect_ratio, focus_mode=focus_mode)
    final_path = render_result["final_clip_path"]

    if include_captions and caption_style != "none":
        try:
            transcript = load_transcript(job_id)
            captioned_path = final_path.replace("_final.mp4", "_captioned.mp4")
            burn_captions(clip, transcript, final_path, captioned_path, style=caption_style)
            shutil.copy(captioned_path, export_path)
        except Exception:
            # Caption burn failed — export without captions
            shutil.copy(final_path, export_path)
    else:
        shutil.copy(final_path, export_path)

    # Validate output exists and is non-trivial.
    if not Path(export_path).exists() or Path(export_path).stat().st_size < 1024:
        raise RuntimeError(f"Export produced empty or missing file: {export_path}")

    return export_path
