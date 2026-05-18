import os
import shutil
import logging
from pathlib import Path
from typing import Optional

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
    hook_y_pct = options.get("hook_y_pct")
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

    # Pre-render hook PNG so we can fold the overlay into the SAME ffmpeg
    # invocation as cut + aspect + captions — eliminates a second full
    # re-encode and roughly halves wall-clock for hooked exports on
    # Railway shared vCPU.
    hook_png_path: Optional[str] = None
    hook_overlay_x = 0
    hook_overlay_y = 0
    if hook_text:
        from services.hook_overlay import create_hook_image
        # Output canvas size depends on aspect_ratio. render_and_caption_clip
        # always produces 1080-wide for 9:16/1:1/square_in_vertical; for 16:9
        # height is normalized to 1080 with proportional width.
        if aspect_ratio in ("9:16", "square_in_vertical"):
            out_w, out_h = 1080, 1920
        elif aspect_ratio == "1:1":
            out_w, out_h = 1080, 1080
        else:  # 16:9 / native — common 1920x1080
            out_w, out_h = 1920, 1080

        target_box_w = int(out_w * 0.9)
        hook_png_path = str(clips_dir / f"clip_{rank:03d}_hook.png")
        try:
            _, box_w, box_h = create_hook_image(
                hook_text, target_box_w, hook_png_path,
                font_scale=hook_font_scale, style=hook_style,
            )
            hook_overlay_x = (out_w - box_w) // 2
            if hook_y_pct is not None:
                pct = max(0.0, min(100.0, float(hook_y_pct))) / 100.0
                hook_overlay_y = int(pct * out_h)
                hook_overlay_y = max(0, min(out_h - box_h, hook_overlay_y))
            elif aspect_ratio == "square_in_vertical":
                margin = 30
                square_top = max(0, (out_h - out_w) // 2)
                square_bot = square_top + out_w
                if hook_position == "center":
                    hook_overlay_y = (out_h - box_h) // 2
                elif hook_position == "bottom":
                    hook_overlay_y = max(square_top, square_bot - box_h - margin)
                else:
                    hook_overlay_y = square_top + margin
            else:
                if hook_position == "center":
                    hook_overlay_y = (out_h - box_h) // 2
                elif hook_position == "bottom":
                    hook_overlay_y = int(out_h * 0.70)
                else:
                    hook_overlay_y = int(out_h * 0.10)
            logger.info("hook overlay: clip=%s style=%s pos=(%d,%d) box=%dx%d",
                        clip.get("rank"), hook_style,
                        hook_overlay_x, hook_overlay_y, box_w, box_h)
        except Exception as exc:
            logger.exception("hook PNG render failed for clip %s", clip.get("rank"))
            raise RuntimeError(f"hook overlay failed: {exc}") from exc

    # Single-pass: cut + aspect + hook overlay + captions in one ffmpeg call
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
        hook_png_path=hook_png_path,
        hook_overlay_x=hook_overlay_x,
        hook_overlay_y=hook_overlay_y,
    )
    if result["error"]:
        raise RuntimeError(f"Export render failed: {result['error']}")

    if hook_png_path and os.path.exists(hook_png_path):
        try:
            os.remove(hook_png_path)
        except OSError:
            pass

    shutil.copy(result["final_clip_path"], export_path)

    if not Path(export_path).exists() or Path(export_path).stat().st_size < 1024:
        raise RuntimeError(f"Export produced empty or missing file: {export_path}")

    if r2.is_enabled():
        key = f"jobs/{job_id}/clips/clip_{rank:03d}_export.mp4"
        r2.upload_in_background(export_path, key)

    return export_path
