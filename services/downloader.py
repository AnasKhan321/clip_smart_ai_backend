import os
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from services.media_tools import ffmpeg_path, ffprobe_path


def get_job_dir(job_id: str) -> Path:
    storage = os.getenv("STORAGE_PATH", "./storage")
    path = Path(storage) / "jobs" / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_video(url: str, job_id: str, progress_callback=None) -> dict:
    job_dir = get_job_dir(job_id)

    meta = _download_via_webshare(url, job_dir, progress_callback)

    video_path = _find_video_file(job_dir)
    if not video_path:
        raise FileNotFoundError("Download completed but video file not found")

    video_path_abs = str(video_path.resolve())

    if Path(video_path_abs).stat().st_size < 100_000:
        raise RuntimeError("Downloaded file too small — download likely failed")

    if not _has_audio_stream(video_path_abs):
        raise RuntimeError(
            "Downloaded media has no audio stream. Source may be silent or "
            "only a video-only format is available."
        )

    audio_path = job_dir / "audio.wav"
    if _needs_wav_extraction():
        _extract_audio(video_path_abs, str(audio_path))

    try:
        meta["duration"] = _get_duration(video_path_abs)
    except Exception:
        meta["duration"] = 0

    if progress_callback:
        progress_callback(100)

    return {**meta, "video_path": video_path_abs, "audio_path": str(audio_path)}


def _pick_proxy() -> str:
    """Pick a random proxy from WEBSHARE_PROXY_LIST (newline or comma-separated).
    Each entry: host:port:user:pass  OR  host:port (uses WEBSHARE_USER/PASS).
    Falls back to WEBSHARE_HOST/PORT/USER/PASS env vars."""
    import random

    _DEFAULT_PROXIES = (
        "p.webshare.io:80:ryekmtdt-gb-1:npg0qrmoknbj\n"
        "p.webshare.io:80:ryekmtdt-ca-2:npg0qrmoknbj\n"
        "p.webshare.io:80:ryekmtdt-de-3:npg0qrmoknbj\n"
        "p.webshare.io:80:ryekmtdt-fr-4:npg0qrmoknbj\n"
        "p.webshare.io:80:ryekmtdt-au-5:npg0qrmoknbj\n"
        "p.webshare.io:80:ryekmtdt-nl-6:npg0qrmoknbj\n"
        "p.webshare.io:80:ryekmtdt-it-7:npg0qrmoknbj\n"
        "p.webshare.io:80:ryekmtdt-es-8:npg0qrmoknbj\n"
        "p.webshare.io:80:ryekmtdt-be-9:npg0qrmoknbj\n"
        "p.webshare.io:80:ryekmtdt-at-10:npg0qrmoknbj"
    )
    proxy_list = os.getenv("WEBSHARE_PROXY_LIST", _DEFAULT_PROXIES).strip()
    if proxy_list:
        sep = "\n" if "\n" in proxy_list else ","
        entries = [e.strip() for e in proxy_list.split(sep) if e.strip()]
        entry = random.choice(entries)
        parts = entry.split(":")
        if len(parts) == 4:
            host, port, user, pw = parts
            return f"http://{user}:{pw}@{host}:{port}"
        elif len(parts) == 2:
            host, port = parts
            user = os.getenv("WEBSHARE_USER", "")
            pw = os.getenv("WEBSHARE_PASS", "")
            if user and pw:
                return f"http://{user}:{pw}@{host}:{port}"
            return f"http://{host}:{port}"

    host = os.getenv("WEBSHARE_HOST", "")
    port = os.getenv("WEBSHARE_PORT", "80")
    user = os.getenv("WEBSHARE_USER", "")
    pw = os.getenv("WEBSHARE_PASS", "")
    if not host:
        raise RuntimeError("WEBSHARE_HOST not set")
    if user and pw:
        return f"http://{user}:{pw}@{host}:{port}"
    return f"http://{host}:{port}"


def _write_cookies_tempfile() -> Optional[str]:
    import base64, tempfile
    b64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
    if not b64:
        return None
    try:
        content = base64.b64decode(b64).decode("utf-8")
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        f.write(content)
        f.flush()
        f.close()
        return f.name
    except Exception as e:
        print(f"[downloader] failed to decode YTDLP_COOKIES_B64: {e}", flush=True)
        return None


def _download_via_webshare(source_url: str, job_dir: Path, progress_callback=None) -> dict:
    import yt_dlp as ytdlp_lib

    cookie_file = _write_cookies_tempfile()
    out_template = str(job_dir / "original.%(ext)s")

    def _progress_hook(d):
        if d["status"] == "downloading" and progress_callback:
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                progress_callback(int(downloaded / total * 80))

    ydl_opts = {
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook],
        "retries": 3,
        "fragment_retries": 3,
        "quiet": True,
        "no_warnings": True,
    }

    if cookie_file:
        # Cookies → Railway IP gets valid CDN URLs → direct download, no proxy cost
        print("[downloader] mode: cookies (no proxy)", flush=True)
        ydl_opts["cookiefile"] = cookie_file
    else:
        # No cookies → use residential proxy for full download
        proxy_url = _pick_proxy()
        print(f"[downloader] mode: proxy {proxy_url.split('@')[-1]}", flush=True)
        ydl_opts["proxy"] = proxy_url

    with ytdlp_lib.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(source_url, download=True)
        print(f"[downloader] quality: {info.get('width')}x{info.get('height')} {info.get('ext')}", flush=True)

    return {
        "title": info.get("title", "video"),
        "duration": info.get("duration", 0),
    }


def save_uploaded_file(file_bytes: bytes, filename: str, job_id: str) -> dict:
    job_dir = get_job_dir(job_id)
    video_path = job_dir / "original.mp4"

    with open(video_path, "wb") as f:
        f.write(file_bytes)

    if not _has_audio_stream(str(video_path)):
        raise RuntimeError(
            f"Uploaded file '{filename}' has no audio stream. "
            "Captions and analysis require audio — re-encode with audio or upload a different file."
        )

    audio_path = job_dir / "audio.wav"
    if _needs_wav_extraction():
        _extract_audio(str(video_path), str(audio_path))

    duration = _get_duration(str(video_path))
    return {
        "title": filename,
        "duration": duration,
        "video_path": str(video_path),
        "audio_path": str(audio_path),
    }


def _find_video_file(job_dir: Path) -> Optional[Path]:
    for ext in ["mp4", "mkv", "webm", "m4a"]:
        p = job_dir / f"original.{ext}"
        if p.exists():
            return p
    return None


def _has_audio_stream(video_path: str) -> bool:
    try:
        result = subprocess.run(
            [ffprobe_path(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return "audio" in result.stdout
    except Exception:
        return True


def _needs_wav_extraction() -> bool:
    provider = os.getenv("TRANSCRIPTION_PROVIDER", "local").lower()
    return provider not in ("assemblyai", "deepgram", "openai", "groq")


def _extract_audio(video_path: str, audio_path: str):
    p = Path(video_path)
    if not p.exists():
        raise RuntimeError(f"Audio extraction: input file missing at {video_path}")
    size = p.stat().st_size
    if size < 1024:
        raise RuntimeError(
            f"Audio extraction: input file too small ({size} bytes) at {video_path}. "
            "Download likely failed or produced an empty file."
        )

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

    base_cmd = [
        ffmpeg_path(), "-y",
        "-err_detect", "ignore_err",
        "-fflags", "+discardcorrupt",
        "-i", video_path,
        "-vn",
        "-ar", "16000", "-ac", "1", "-f", "wav", audio_path,
    ]
    try:
        subprocess.run(base_cmd, check=True, capture_output=True, timeout=600)
        return
    except subprocess.TimeoutExpired:
        raise RuntimeError("Audio extraction timed out (>10min)")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="ignore").strip()[-500:]

    try:
        subprocess.run(
            [ffmpeg_path(), "-y",
             "-err_detect", "ignore_err",
             "-fflags", "+discardcorrupt",
             "-c:a", "aac_fixed",
             "-i", video_path,
             "-vn",
             "-ar", "16000", "-ac", "1", "-f", "wav", audio_path],
            check=True, capture_output=True, timeout=600,
        )
        return
    except subprocess.TimeoutExpired:
        raise RuntimeError("Audio extraction timed out (>10min)")
    except subprocess.CalledProcessError as e2:
        stderr2 = (e2.stderr or b"").decode("utf-8", errors="ignore").strip()[-500:]
        raise RuntimeError(
            f"Audio extraction failed (ffmpeg exit {e2.returncode}). "
            f"Primary error: {stderr}. Fallback error: {stderr2}"
        ) from e2


def _get_duration(video_path: str) -> float:
    result = subprocess.run(
        [ffprobe_path(), "-v", "quiet", "-print_format", "json", "-show_format", video_path],
        capture_output=True, text=True, timeout=30, check=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"].get("duration", 0))
