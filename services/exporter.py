import os
import shutil
import subprocess
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
    caption_position = options.get("caption_position", "bottom")
    hook_text = (options.get("hook_text") or "").strip()
    hook_position = options.get("hook_position", "top")
    hook_font_scale = float(options.get("hook_font_scale", 1.0))
    hook_style = options.get("hook_style", "serif_card")
    # Focus modes (face/speaker) only make sense when re-framing to vertical.
    # For 16:9 / 1:1 they'd run heavy track computation (face detection on
    # the entire source) for no benefit — the filter chain ignores them.
    if aspect_ratio != "9:16" and focus_mode in ("face", "speaker"):
        focus_mode = "none"

    # Restore source from R2 if worker disk was wiped — must happen before
    # face/speaker track computation (those also read the source video).
    _ensure_local_source(job_id)

    # Compute focus / face track lazily on first export of each mode.
    # For face mode: process ONLY the clip window, not the whole source.
    # Massive speedup on long podcasts where user exports a 30s clip.
    clip_start = clip.get("user_start_seconds") or clip["start_seconds"]
    clip_end = clip.get("user_end_seconds") or clip["end_seconds"]
    face_clip_range = (float(clip_start), float(clip_end))
    if focus_mode == "speaker":
        from services.speaker_focus import compute_focus_track
        try:
            compute_focus_track(job_id)
        except Exception:
            focus_mode = "none"
    elif focus_mode == "face":
        from services.speaker_focus import compute_face_track
        try:
            compute_face_track(job_id, clip_range=face_clip_range)
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
        caption_position=caption_position,
        include_captions=include_captions and transcript is not None,
        transcript=transcript,
        profile=render_profile,
        face_clip_range=face_clip_range if focus_mode == "face" else None,
    )
    if result["error"]:
        raise RuntimeError(f"Export render failed: {result['error']}")

    source_for_copy = result["final_clip_path"]
    if hook_text:
        from services.hook_overlay import add_hook_to_video
        hooked_path = str(Path(source_for_copy).with_name(
            Path(source_for_copy).stem + "_hook.mp4"
        ))
        try:
            logger.info("applying hook overlay: clip=%s style=%s pos=%s scale=%s text=%r",
                        clip.get("rank"), hook_style, hook_position, hook_font_scale, hook_text[:60])
            add_hook_to_video(
                source_for_copy, hook_text, hooked_path,
                position=hook_position, font_scale=hook_font_scale,
                style=hook_style,
            )
            if not Path(hooked_path).exists() or Path(hooked_path).stat().st_size < 1024:
                raise RuntimeError(f"hook overlay produced empty file: {hooked_path}")
            source_for_copy = hooked_path
            logger.info("hook overlay OK: %s", hooked_path)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode(errors="ignore")[-500:]
            logger.error("hook overlay ffmpeg failed for clip %s: %s", clip.get("rank"), stderr)
            raise RuntimeError(f"hook overlay failed: {stderr}") from exc
        except Exception as exc:
            logger.exception("hook overlay failed for clip %s", clip.get("rank"))
            raise RuntimeError(f"hook overlay failed: {exc}") from exc

    shutil.copy(source_for_copy, export_path)

    if not Path(export_path).exists() or Path(export_path).stat().st_size < 1024:
        raise RuntimeError(f"Export produced empty or missing file: {export_path}")

    if r2.is_enabled():
        key = f"jobs/{job_id}/clips/clip_{rank:03d}_export.mp4"
        r2.upload_in_background(export_path, key)

    return export_path
