import os
import shutil
import logging
import subprocess
import re
from pathlib import Path
from typing import Optional

from services.editor import render_and_caption_clip
from services.transcriber import load_transcript
from services import r2
from services.media_tools import ffmpeg_path, encoder_audio_opts, build_audio_mix_filter_complex

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


def _fetch_music_file(music_track_id: str) -> str:
    """Download music file from R2, return local path.

    Validates music_track_id to prevent path traversal attacks.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', music_track_id):
        raise ValueError(f"Invalid music_track_id: must be alphanumeric/dash/underscore, max 64 chars")

    if not r2.is_enabled():
        raise ValueError("R2 storage not configured")

    storage = os.getenv("STORAGE_PATH", "./storage")
    music_dir = Path(storage) / "music"
    music_dir.mkdir(parents=True, exist_ok=True)

    local_path = music_dir / f"{music_track_id}.mp3"

    # Verify path is within music_dir (prevent traversal)
    resolved_path = local_path.resolve()
    resolved_music_dir = music_dir.resolve()
    if not str(resolved_path).startswith(str(resolved_music_dir) + os.sep):
        raise ValueError("Path traversal attempt detected")

    if local_path.exists() and local_path.stat().st_size > 0:
        return str(local_path)

    r2_key = f"music/{music_track_id}.mp3"
    if not r2.object_exists(r2_key):
        raise FileNotFoundError(f"Music track not found in R2: {r2_key}")

    r2.download_file(r2_key, str(local_path))
    return str(local_path)


def _mix_audio_and_reencode(
    video_path: str,
    music_path: str,
    music_volume: float,
    music_fade_in: float,
    music_fade_out: float,
    music_trim_start: float = 0,
    music_trim_end: float = 0,
    clip_duration: float = 0,
) -> str:
    """Mix original audio from video with music track using FFmpeg.

    Args:
        video_path: Path to video file (with original audio)
        music_path: Path to music MP3 file
        music_volume: Volume multiplier for music (0-1)
        music_fade_in: Fade in duration in seconds
        music_fade_out: Fade out duration in seconds
        music_trim_start: Trim music from start (seconds)
        music_trim_end: Trim music to end (seconds, 0 = full duration)
        clip_duration: Duration of clip in seconds (for fade_out calculation)

    Returns:
        Path to output MP4 with mixed audio
    """
    output_path = str(Path(video_path).parent / f"{Path(video_path).stem}_mixed.mp4")

    filter_complex = build_audio_mix_filter_complex(
        music_volume=music_volume,
        music_fade_in=music_fade_in,
        music_fade_out=music_fade_out,
        music_trim_start=music_trim_start,
        music_trim_end=music_trim_end,
        clip_duration=clip_duration,
    )

    cmd = [
        ffmpeg_path(),
        "-y",
        "-i", video_path,
        "-i", music_path,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[audio]",
        "-c:v", "copy",
        *encoder_audio_opts(),
        output_path,
    ]

    logger.info("mixing audio: cmd=%s", " ".join(cmd[:5]))
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    except subprocess.CalledProcessError as exc:
        logger.error("audio mix failed: stdout=%s stderr=%s", exc.stdout, exc.stderr)
        raise RuntimeError(f"Audio mixing failed: {exc.stderr.decode()[:500]}") from exc

    if not Path(output_path).exists() or Path(output_path).stat().st_size < 1024:
        raise RuntimeError(f"Audio mix produced empty file: {output_path}")

    return output_path


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
    music_enabled = options.get("music_enabled", False)
    music_track_id = options.get("music_track_id")
    music_volume = float(options.get("music_volume", 0.5))
    music_fade_in = float(options.get("music_fade_in", 0))
    music_fade_out = float(options.get("music_fade_out", 0))
    music_trim_start = float(options.get("music_trim_start", 0))
    music_trim_end = float(options.get("music_trim_end", 0))
    scene_template_id = options.get("scene_template_id")
    # Scene templates place the clip inside a 16:9 TV/tablet screen cutout —
    # feeding them an already-vertical 9:16 reframe would crop it a second
    # time. Force 16:9 so the template gets the clip's natural aspect.
    if scene_template_id:
        aspect_ratio = "16:9"
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
    # Scene templates reserve a clean margin around the video slot for the
    # hook (that's the whole point of the template art) — baking the hook
    # onto the pre-warp 16:9 clip means the cover-fit-crop into the template's
    # (usually narrower) screen slot chops its left/right edges. Render the
    # hook onto the final templated canvas instead, in its clean zone.
    hook_after_template = bool(scene_template_id and hook_text)

    hook_png_path: Optional[str] = None
    hook_overlay_x = 0
    hook_overlay_y = 0
    if hook_text and not hook_after_template:
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

    # Mix audio if music enabled
    final_video_path = result["final_clip_path"]
    if music_enabled and music_track_id:
        try:
            music_file = _fetch_music_file(music_track_id)
            clip_duration = (clip.get("user_end_seconds") or clip["end_seconds"]) - \
                           (clip.get("user_start_seconds") or clip["start_seconds"])
            final_video_path = _mix_audio_and_reencode(
                final_video_path,
                music_file,
                music_volume=music_volume,
                music_fade_in=music_fade_in,
                music_fade_out=music_fade_out,
                music_trim_start=music_trim_start,
                music_trim_end=music_trim_end,
                clip_duration=float(clip_duration),
            )
            logger.info("audio mixed: clip=%s volume=%s fade_in=%s fade_out=%s trim=%s-%s",
                        clip.get("rank"), music_volume, music_fade_in, music_fade_out,
                        music_trim_start, music_trim_end)
        except Exception as exc:
            logger.warning("audio mixing failed (continuing without music): %s", exc)

    if scene_template_id:
        from services.scene_template import apply_scene_template
        templated_path = str(clips_dir / f"clip_{rank:03d}_templated.mp4")
        try:
            apply_scene_template(final_video_path, scene_template_id, templated_path)
            final_video_path = templated_path
            logger.info("scene template applied: clip=%s template=%s",
                        clip.get("rank"), scene_template_id)
        except Exception as exc:
            logger.exception("scene template failed for clip %s", clip.get("rank"))
            raise RuntimeError(f"scene template failed: {exc}") from exc

    if hook_after_template:
        from services.hook_overlay import add_hook_to_video
        hooked_path = str(clips_dir / f"clip_{rank:03d}_hooked.mp4")
        try:
            add_hook_to_video(
                final_video_path, hook_text, hooked_path,
                position=hook_position, font_scale=hook_font_scale,
                style=hook_style, y_pct=hook_y_pct,
            )
            final_video_path = hooked_path
            logger.info("hook applied post-template: clip=%s style=%s",
                        clip.get("rank"), hook_style)
        except Exception as exc:
            logger.exception("post-template hook overlay failed for clip %s", clip.get("rank"))
            raise RuntimeError(f"hook overlay failed: {exc}") from exc

    shutil.copy(final_video_path, export_path)

    if not Path(export_path).exists() or Path(export_path).stat().st_size < 1024:
        raise RuntimeError(f"Export produced empty or missing file: {export_path}")

    if r2.is_enabled():
        key = f"jobs/{job_id}/clips/clip_{rank:03d}_export.mp4"
        r2.upload_in_background(export_path, key)

    return export_path
