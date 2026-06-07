import os
import json
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


def _write_cookies_tempfile() -> Optional[str]:
    """Decode YTDLP_COOKIES_B64 env var → temp file. Returns path or None."""
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


def _pick_proxy() -> str:
    """Pick a proxy URL from WEBSHARE_PROXY_LIST (comma-separated host:port entries)
    or fall back to single WEBSHARE_HOST/PORT. Rotates randomly across all entries."""
    import random
    user = os.getenv("WEBSHARE_USER", "")
    pw = os.getenv("WEBSHARE_PASS", "")

    proxy_list = os.getenv("WEBSHARE_PROXY_LIST", "").strip()
    if proxy_list:
        entries = [e.strip() for e in proxy_list.split(",") if e.strip()]
        host_port = random.choice(entries)
    else:
        host = os.getenv("WEBSHARE_HOST", "")
        port = os.getenv("WEBSHARE_PORT", "80")
        if not host:
            raise RuntimeError("WEBSHARE_HOST not set")
        host_port = f"{host}:{port}"

    if user and pw:
        return f"http://{user}:{pw}@{host_port}"
    return f"http://{host_port}"


def _download_via_webshare(source_url: str, job_dir: Path, progress_callback=None) -> dict:
    proxy_url = _pick_proxy()
    print(f"[downloader] proxy: {proxy_url.split('@')[-1]}", flush=True)  # log host:port only

    ytdlp_bin = shutil.which("yt-dlp") or "yt-dlp"

    # Write cookies from env var to a temp file if available
    cookie_file = _write_cookies_tempfile()

    cmd = [
        ytdlp_bin,
        "--proxy", proxy_url,
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--print", "%(width)sx%(height)s %(ext)s %(format_note)s",
        "--get-url",
        "--no-warnings",
    ]
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    cmd.append(source_url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp --get-url exit {result.returncode}: {result.stderr.strip()[-300:]}")

    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    # --print outputs one info line, --get-url outputs CDN URLs. Split them.
    cdn_urls = [l for l in lines if l.startswith("http")]
    info_lines = [l for l in lines if not l.startswith("http")]
    if info_lines:
        print(f"[downloader] quality: {info_lines[0]}", flush=True)
    if not cdn_urls:
        raise RuntimeError("yt-dlp --get-url returned no URLs")

    video_cdn = cdn_urls[0]
    audio_cdn = cdn_urls[1] if len(cdn_urls) > 1 else None

    yt_ua = "com.google.android.youtube/19.09.37 (Linux; U; Android 14)"

    def _fetch(src: str, dst: Path):
        req = urllib.request.Request(src, headers={"User-Agent": yt_ua})
        with urllib.request.urlopen(req, timeout=600) as r:
            with open(dst, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)

    if progress_callback:
        progress_callback(10)

    if audio_cdn:
        vid_path = job_dir / "_ws_video.mp4"
        aud_path = job_dir / "_ws_audio.m4a"
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(_fetch, video_cdn, vid_path), ex.submit(_fetch, audio_cdn, aud_path)]
            for f in futs:
                f.result()
        if progress_callback:
            progress_callback(70)
        out_path = job_dir / "original.mp4"
        subprocess.run(
            [ffmpeg_path(), "-y", "-i", str(vid_path), "-i", str(aud_path),
             "-c", "copy", "-movflags", "+faststart", str(out_path)],
            check=True, capture_output=True, timeout=600,
        )
        for p in (vid_path, aud_path):
            try:
                p.unlink()
            except OSError:
                pass
    else:
        out_path = job_dir / "original.mp4"
        _fetch(video_cdn, out_path)
        if progress_callback:
            progress_callback(70)

    return {"title": "video", "duration": 0}


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
