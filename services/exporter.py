import os
import shutil
import logging
from pathlib import Path

from services.editor import render_and_caption_clip
from services.transcriber import load_transcript
from services import r2

logger = logging.getLogger(__name__)


def _ensure_local_source(job_id: str) -> Path:
    """Make sure the source video exists on local worker disk.

    Worker disk is ephemeral on Railway — after a redeploy or task re-delivery
    to a different worker, `storage/jobs/{id}/original.mp4` may be gone even
    though the job already finished its earlier stages. Pull it back from R2.
    """
    storage = os.getenv("STORAGE_PATH", "./storage")
    job_dir = Path(storage) / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    local = job_dir / "original.mp4"
    if local.exists() and local.stat().st_size > 0:
        return local

    # Try R2 with each candidate extension; mp4 is the convention.
    if not r2.is_enabled():
        raise FileNotFoundError(
            f"source not found locally and R2 not configured: {local}"
        )
    for ext in ("mp4", "mkv", "webm", "m4a"):
        key = f"jobs/{job_id}/original.{ext}"
        try:
            if r2.object_exists(key):
                target = job_dir / f"original.{ext}"
                r2.download_file(key, str(target))
                # Always present an `original.mp4` alias so downstream code
                # (editor.py etc.) finds the expected name.
                if ext != "mp4":
                    mp4_alias = job_dir / "original.mp4"
                    if not mp4_alias.exists():
                        try:
                            mp4_alias.symlink_to(target.name)
                        except Exception:
                            shutil.copy(target, mp4_alias)
                    return mp4_alias
                return target
        except Exception as exc:
            logger.warning("r2 source restore probe failed (%s): %s", key, exc)

    raise FileNotFoundError(
        f"source not found in R2 either: jobs/{job_id}/original.*"
    )


def export_clip(job_id: str, clip: dict, options: dict) -> str:
    aspect_ratio = options.get("aspect_ratio", "9:16")
    include_captions = options.get("include_captions", True)
    caption_style = options.get("caption_style", "word_highlight")
    focus_mode = options.get("focus_mode", "none")

    # Restore source from R2 if worker disk was wiped — must happen before
    # face/speaker track computation (those also read the source video).
    _ensure_local_source(job_id)

    # Compute focus / face track lazily on first export of each mode.
    if focus_mode == "speaker":
        from services.speaker_focus import compute_focus_track
        try:
            compute_focus_track(job_id)
        except Exception:
            focus_mode = "none"
    elif focus_mode == "face":
        from services.speaker_focus import compute_face_track
        try:
            compute_face_track(job_id)
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
    render_profile = "face_export" if focus_mode == "face" else "export"
    result = render_and_caption_clip(
        job_id, clip,
        aspect_ratio=aspect_ratio,
        focus_mode=focus_mode,
        caption_style=caption_style,
        include_captions=include_captions and transcript is not None,
        transcript=transcript,
        profile=render_profile,
    )
    if result["error"]:
        raise RuntimeError(f"Export render failed: {result['error']}")

    shutil.copy(result["final_clip_path"], export_path)

    if not Path(export_path).exists() or Path(export_path).stat().st_size < 1024:
        raise RuntimeError(f"Export produced empty or missing file: {export_path}")

    if r2.is_enabled():
        key = f"jobs/{job_id}/clips/clip_{rank:03d}_export.mp4"
        r2.upload_in_background(export_path, key)

    return export_path
