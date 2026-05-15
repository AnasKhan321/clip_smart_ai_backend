import os
import json
import random
import time
import subprocess
import yt_dlp
from pathlib import Path
from services.media_tools import ffmpeg_path, ffprobe_path

# Ensure Homebrew Node.js is on PATH so yt-dlp-ejs can solve n-challenge
_homebrew_bin = "/opt/homebrew/bin"
if _homebrew_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _homebrew_bin + ":" + os.environ.get("PATH", "")


def get_job_dir(job_id: str) -> Path:
    storage = os.getenv("STORAGE_PATH", "./storage")
    path = Path(storage) / "jobs" / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


_BACKEND_DIR = Path(__file__).resolve().parent.parent  # backend/


from typing import Optional

def _resolve_cookie_path(p: str) -> Optional[str]:
    """Resolve relative cookie paths against backend dir + common roots."""
    pth = Path(p).expanduser()
    if pth.is_absolute():
        return str(pth) if pth.is_file() else None
    candidates = [
        Path.cwd() / pth,
        _BACKEND_DIR / pth,
        _BACKEND_DIR.parent / pth,  # clipforge/
    ]
    for c in candidates:
        c = c.resolve()
        if c.is_file():
            return str(c)
    return None


def _get_cookie_file() -> Optional[str]:
    """
    Returns path to Netscape-format cookie file, or None.
    YTDLP_COOKIE_FILES=a.txt,b.txt  → picks one at random (rotation)
    YTDLP_COOKIE_FILE=a.txt         → single file
    Relative paths resolved against backend dir + project root.
    """
    multi = os.getenv("YTDLP_COOKIE_FILES", "").strip()
    if multi:
        resolved = [r for r in (_resolve_cookie_path(p.strip()) for p in multi.split(",") if p.strip()) if r]
        if resolved:
            return random.choice(resolved)

    single = os.getenv("YTDLP_COOKIE_FILE", "").strip()
    if single:
        return _resolve_cookie_path(single)

    # Auto-discover: clipforge/cookies/*.txt
    cookies_dir = _BACKEND_DIR.parent / "cookies"
    if cookies_dir.is_dir():
        files = sorted(cookies_dir.glob("*.txt"))
        if files:
            return str(random.choice(files))

    return None


def download_video(url: str, job_id: str, progress_callback=None) -> dict:
    job_dir = get_job_dir(job_id)
    output_template = str(job_dir / "original.%(ext)s")

    def ydl_progress_hook(d):
        if d["status"] == "downloading" and progress_callback:
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                progress_callback(int(downloaded / total * 80))

    node_bin = os.getenv("NODE_BIN", "/opt/homebrew/bin/node")
    js_runtimes = {"node": {"path": node_bin}} if os.path.isfile(node_bin) else {}

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestvideo+bestaudio/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "writeinfojson": True,
        "progress_hooks": [ydl_progress_hook],
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {"youtube": {"player_client": ["web", "tv_downgraded", "web_safari"]}},
        "js_runtimes": js_runtimes,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

    cookie_file = _get_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
        print(f"[downloader] using cookie file: {cookie_file}", flush=True)
    else:
        print("[downloader] WARNING: no cookie file found — YouTube may block as bot", flush=True)

    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                meta = {
                    "title": info.get("title", "Untitled"),
                    "duration": info.get("duration"),
                    "uploader": info.get("uploader"),
                    "language": info.get("language"),
                }
            break
        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            if "Private video" in err or "unavailable" in err.lower():
                raise ValueError("Video is private or unavailable")
            if "age" in err.lower():
                raise ValueError("Age-restricted content cannot be downloaded")
            if "Sign in to confirm" in err or "not a bot" in err:
                if not cookie_file:
                    raise ValueError(
                        "YouTube blocked the download (bot check). No cookie file was found. "
                        "Add a Netscape-format cookie export to clipforge/cookies/ and retry."
                    )
                if attempt == 2:
                    raise ValueError(
                        f"YouTube blocked even with cookies ({cookie_file}). "
                        "Cookies may be expired — re-export from a logged-in browser session."
                    )
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)

    video_path = _find_video_file(job_dir)
    if not video_path:
        raise FileNotFoundError("Download completed but video file not found")

    audio_path = job_dir / "audio.wav"
    _extract_audio(str(video_path), str(audio_path))

    if progress_callback:
        progress_callback(100)

    return {
        **meta,
        "video_path": str(video_path),
        "audio_path": str(audio_path),
    }


def save_uploaded_file(file_bytes: bytes, filename: str, job_id: str) -> dict:
    job_dir = get_job_dir(job_id)
    video_path = job_dir / "original.mp4"

    with open(video_path, "wb") as f:
        f.write(file_bytes)

    audio_path = job_dir / "audio.wav"
    _extract_audio(str(video_path), str(audio_path))

    duration = _get_duration(str(video_path))
    return {
        "title": filename,
        "duration": duration,
        "video_path": str(video_path),
        "audio_path": str(audio_path),
    }


def _find_video_file(job_dir: Path):
    for ext in ["mp4", "mkv", "webm", "m4a"]:
        p = job_dir / f"original.{ext}"
        if p.exists():
            return p
    return None


def _extract_audio(video_path: str, audio_path: str):
    # Validate input before invoking ffmpeg — empty/missing file is the most
    # common cause of exit 234 (EINVAL) on deployment.
    p = Path(video_path)
    if not p.exists():
        raise RuntimeError(f"Audio extraction: input file missing at {video_path}")
    size = p.stat().st_size
    if size < 1024:
        raise RuntimeError(
            f"Audio extraction: input file too small ({size} bytes) at {video_path}. "
            "Download likely failed or produced an empty file."
        )

    # Probe streams — fail fast with a clear message if there's no audio track.
    try:
        probe = subprocess.run(
            [ffprobe_path(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, check=True, timeout=30,
        )
        if "audio" not in probe.stdout:
            raise RuntimeError(
                f"Audio extraction: source video has no audio stream ({video_path})"
            )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()[-300:]
        raise RuntimeError(f"Audio extraction: ffprobe failed: {stderr}") from e

    try:
        subprocess.run(
            [ffmpeg_path(), "-y", "-i", video_path,
             "-vn",                  # ignore video stream
             "-ar", "16000", "-ac", "1", "-f", "wav", audio_path],
            check=True, capture_output=True, timeout=600,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="ignore").strip()[-500:]
        raise RuntimeError(
            f"Audio extraction failed (ffmpeg exit {e.returncode}): {stderr}"
        ) from e
    except subprocess.TimeoutExpired:
        raise RuntimeError("Audio extraction timed out (>10min)")


def _get_duration(video_path: str) -> float:
    result = subprocess.run(
        [ffprobe_path(), "-v", "quiet", "-print_format", "json", "-show_format", video_path],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"].get("duration", 0))
