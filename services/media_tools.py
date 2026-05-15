import os
import shutil
from pathlib import Path


class MediaToolMissingError(RuntimeError):
    pass


from typing import Optional

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


def media_tools_status() -> dict:
    status = {}
    for name, resolver in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
        try:
            status[name] = {"ok": True, "path": resolver()}
        except MediaToolMissingError as exc:
            status[name] = {"ok": False, "path": None, "error": str(exc)}
    return status
