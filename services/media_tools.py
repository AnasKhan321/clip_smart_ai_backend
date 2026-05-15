import functools
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


class MediaToolMissingError(RuntimeError):
    pass


def _first_existing(candidates: list[str]) -> Optional[str]:
    for candidate in candidates:
        if not candidate:
            continue
        expanded = os.path.expanduser(candidate)
        if Path(expanded).is_file():
            return expanded
        found = shutil.which(expanded)
        if found:
            return found
    return None


def ffmpeg_path(require_libass: bool = False) -> str:
    configured = os.getenv("FFMPEG_BIN", "").strip()
    candidates = [
        configured,
        "~/bin/ffmpeg-libass" if require_libass else "",
        "ffmpeg",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/snap/bin/ffmpeg",
    ]
    path = _first_existing(candidates)
    if path:
        return path
    raise MediaToolMissingError(
        "FFmpeg is not installed or not on PATH. Install it with: sudo apt update && sudo apt install -y ffmpeg. "
        "If you installed it somewhere custom, set FFMPEG_BIN=/full/path/to/ffmpeg in backend/.env."
    )


def ffprobe_path() -> str:
    configured = os.getenv("FFPROBE_BIN", "").strip()
    ffmpeg_bin = os.getenv("FFMPEG_BIN", "").strip()
    sibling = ""
    if ffmpeg_bin:
        p = Path(os.path.expanduser(ffmpeg_bin))
        sibling = str(p.with_name("ffprobe"))

    libass_sibling = str(Path(os.path.expanduser("~/bin/ffmpeg-libass")).with_name("ffprobe"))
    candidates = [
        configured,
        sibling,
        libass_sibling,
        "ffprobe",
        "/usr/bin/ffprobe",
        "/usr/local/bin/ffprobe",
        "/snap/bin/ffprobe",
    ]
    path = _first_existing(candidates)
    if path:
        return path
    raise MediaToolMissingError(
        "FFprobe is not installed or not on PATH. It is included with FFmpeg: sudo apt update && sudo apt install -y ffmpeg. "
        "If you installed it somewhere custom, set FFPROBE_BIN=/full/path/to/ffprobe in backend/.env."
    )


def _encoder_actually_works(encoder: str) -> bool:
    """Run a tiny dummy encode to verify the encoder works at runtime.

    Compile-time presence (ffmpeg -encoders) does not imply GPU/hardware
    availability. NVENC and friends list in -encoders even on machines
    without the actual hardware, and only fail at encode time. We catch
    that here once, cached for process lifetime.
    """
    try:
        result = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1:r=1",
             "-c:v", encoder, "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def detect_hwaccel() -> Optional[str]:
    """Detect a working H.264 hardware encoder. Cached for process lifetime.

    Preference order: videotoolbox (macOS) > nvenc (NVIDIA) > qsv (Intel) > amf (AMD).
    Each candidate is BOTH compile-checked and run-tested before being chosen,
    so a binary that advertises nvenc on a non-GPU host falls back cleanly.
    Override with ENCODER=software to force libx264, or ENCODER=<name> to pin.
    """
    forced = os.getenv("ENCODER", "auto").lower()
    if forced == "software":
        return None
    try:
        result = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        compiled = result.stdout
    except Exception:
        return None

    if forced not in ("auto", ""):
        # Caller pinned a specific encoder — trust them, just verify it runs.
        if forced in compiled and _encoder_actually_works(forced):
            return forced
        return None

    for enc in ("h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf"):
        if enc in compiled and _encoder_actually_works(enc):
            return enc
    return None


def encoder_video_opts(profile: str = "preview") -> list[str]:
    """FFmpeg -c:v + tuning args. profile: 'preview' (fast) or 'export' (quality)."""
    hw = detect_hwaccel()
    if hw == "h264_videotoolbox":
        bitrate = "8M" if profile == "export" else "4M"
        return ["-c:v", "h264_videotoolbox", "-b:v", bitrate, "-allow_sw", "1",
                "-pix_fmt", "yuv420p"]
    if hw == "h264_nvenc":
        cq = "20" if profile == "export" else "26"
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq",
                "-cq", cq, "-pix_fmt", "yuv420p"]
    if hw == "h264_qsv":
        q = "20" if profile == "export" else "26"
        return ["-c:v", "h264_qsv", "-global_quality", q, "-pix_fmt", "yuv420p"]
    if hw == "h264_amf":
        q = "20" if profile == "export" else "26"
        return ["-c:v", "h264_amf", "-quality", "balanced", "-qp_i", q,
                "-pix_fmt", "yuv420p"]
    # libx264 fallback
    crf = "20" if profile == "export" else "26"
    preset = "medium" if profile == "export" else "ultrafast"
    return ["-c:v", "libx264", "-crf", crf, "-preset", preset, "-pix_fmt", "yuv420p"]


def encoder_audio_opts() -> list[str]:
    return ["-c:a", "aac", "-b:a", "192k"]


def media_tools_status() -> dict:
    status = {}
    for name, resolver in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
        try:
            status[name] = {"ok": True, "path": resolver()}
        except MediaToolMissingError as exc:
            status[name] = {"ok": False, "path": None, "error": str(exc)}
    return status
