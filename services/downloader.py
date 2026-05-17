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

    # Try RapidAPI first if configured. Most reliable — direct YouTube CDN URLs.
    rapidapi_key = os.getenv("RAPIDAPI_YT_KEY")
    if rapidapi_key and ("youtube.com" in url or "youtu.be" in url):
        try:
            meta = _download_via_rapidapi(rapidapi_key, url, job_dir, progress_callback)
            video_path = _find_video_file(job_dir)
            if video_path:
                video_path_abs = str(Path(video_path).resolve())
                if Path(video_path_abs).stat().st_size > 100_000 and _has_audio_stream(video_path_abs):
                    audio_path = (job_dir / "audio.wav").resolve()
                    if _needs_wav_extraction():
                        _extract_audio(video_path_abs, str(audio_path))
                    try:
                        meta["duration"] = _get_duration(video_path_abs)
                    except Exception:
                        meta["duration"] = 0
                    if progress_callback:
                        progress_callback(100)
                    return {**meta, "video_path": video_path_abs, "audio_path": str(audio_path)}
        except Exception as exc:
            print(f"[downloader] rapidapi failed ({exc}), trying next", flush=True)

    # Try cobalt next if configured. Bypasses yt-dlp's YouTube cookie hell.
    cobalt_url = os.getenv("COBALT_API_URL")
    if cobalt_url:
        try:
            meta = _download_via_cobalt(cobalt_url, url, job_dir, progress_callback)
            video_path = _find_video_file(job_dir)
            if video_path:
                video_path_abs = str(Path(video_path).resolve())
                file_size = Path(video_path_abs).stat().st_size
                if file_size < 100_000:
                    print(f"[downloader] cobalt returned tiny/empty file ({file_size} bytes), removing and falling back", flush=True)
                    Path(video_path_abs).unlink(missing_ok=True)
                    raise RuntimeError("cobalt output too small")
                if _has_audio_stream(video_path_abs):
                    audio_path = (job_dir / "audio.wav").resolve()
                    if _needs_wav_extraction():
                        _extract_audio(video_path_abs, str(audio_path))
                    try:
                        meta["duration"] = _get_duration(video_path_abs)
                    except Exception as exc:
                        print(f"[downloader] duration probe failed ({exc}), defaulting to 0", flush=True)
                        meta["duration"] = 0
                    if progress_callback:
                        progress_callback(100)
                    return {**meta, "video_path": video_path_abs, "audio_path": str(audio_path)}
            print("[downloader] cobalt returned file without audio, falling back to yt-dlp", flush=True)
        except Exception as exc:
            print(f"[downloader] cobalt failed ({exc}), falling back to yt-dlp", flush=True)

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
        # Require audio stream — fallbacks default to "best" which can grab
        # a video-only mp4 on some sources, then AssemblyAI rejects it with
        # "File does not appear to contain audio".
        "format": "bestvideo+bestaudio/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[acodec!=none][ext=mp4]/best[acodec!=none]",
        "merge_output_format": "mp4",
        "writeinfojson": True,
        "progress_hooks": [ydl_progress_hook],
        "retries": 3,
        "fragment_retries": 3,
        # Multiple player clients = yt-dlp tries each. Android/iOS clients
        # often work without fresh cookies because they use mobile API.
        "extractor_args": {"youtube": {"player_client": [
            "android", "ios", "web_safari", "tv_downgraded", "web"
        ]}},
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

    if not _has_audio_stream(str(video_path)):
        raise RuntimeError(
            "Downloaded media has no audio stream. Source may be silent or "
            "only a video-only format is available. Try a different URL."
        )

    audio_path = job_dir / "audio.wav"
    # AssemblyAI accepts mp4 directly — skip the local WAV extraction (saves
    # ~30s of ffmpeg work + ~10× upload size). Other providers (local whisper,
    # openai, pyannote diarizer) still need a separate WAV.
    if _needs_wav_extraction():
        _extract_audio(str(video_path), str(audio_path))

    if progress_callback:
        progress_callback(100)

    return {
        **meta,
        "video_path": str(video_path),
        "audio_path": str(audio_path),
    }


_YOUTUBE_ID_RE = None


def _extract_youtube_id(url: str) -> Optional[str]:
    import re
    global _YOUTUBE_ID_RE
    if _YOUTUBE_ID_RE is None:
        _YOUTUBE_ID_RE = re.compile(
            r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/|v/)|youtu\.be/)([\w-]{11})"
        )
    m = _YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


def _download_via_rapidapi(api_key: str, source_url: str, job_dir: Path,
                            progress_callback=None) -> dict:
    """RapidAPI ytstream → direct YouTube CDN URLs → ffmpeg mux locally.

    Downloads best mp4 video (≤720p) + best mp4 audio, muxes to original.mp4.
    """
    import urllib.request

    video_id = _extract_youtube_id(source_url)
    if not video_id:
        raise RuntimeError(f"could not extract YouTube ID from {source_url}")

    req = urllib.request.Request(
        f"https://ytstream-download-youtube-videos.p.rapidapi.com/dl?id={video_id}",
        headers={
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": "ytstream-download-youtube-videos.p.rapidapi.com",
            "User-Agent": "Mozilla/5.0 (compatible; clipforge/1.0)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if data.get("status") != "OK":
        raise RuntimeError(f"rapidapi status={data.get('status')}: {data.get('error', 'unknown')}")

    title = data.get("title", "video")
    duration = int(data.get("lengthSeconds", 0) or 0)

    # Pick mp4 video ≤ 720p (avc1 codec for compatibility).
    video_fmts = [
        f for f in data.get("adaptiveFormats", [])
        if "video/mp4" in f.get("mimeType", "") and "avc1" in f.get("mimeType", "")
    ]
    # Sort by height ascending, take largest ≤720
    def _height(f):
        try:
            return int(f.get("qualityLabel", "0").rstrip("p"))
        except ValueError:
            return 0
    video_fmts.sort(key=_height)
    video_fmt = next((f for f in reversed(video_fmts) if _height(f) <= 720), video_fmts[-1] if video_fmts else None)
    if not video_fmt:
        raise RuntimeError("no suitable mp4 video format in rapidapi response")

    audio_fmts = [
        f for f in data.get("adaptiveFormats", [])
        if "audio/mp4" in f.get("mimeType", "")
    ]
    audio_fmts.sort(key=lambda f: int(f.get("bitrate", 0) or 0))
    audio_fmt = audio_fmts[-1] if audio_fmts else None
    if not audio_fmt:
        raise RuntimeError("no mp4 audio format in rapidapi response")

    vid_path = job_dir / "_dl_video.mp4"
    aud_path = job_dir / "_dl_audio.m4a"

    def _fetch(src_url: str, dst: Path):
        # YouTube CDN rejects requests with default Python-urllib UA. Use
        # browser-like UA matching the one player_client=android would send.
        fetch_req = urllib.request.Request(
            src_url,
            headers={"User-Agent": "com.google.android.youtube/19.09.37 (Linux; U; Android 14)"},
        )
        with urllib.request.urlopen(fetch_req, timeout=600) as r:
            with open(dst, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)

    # Parallel download — video and audio streams are independent CDN URLs,
    # downloading sequentially wastes wall time. Threads here are fine since
    # the work is network I/O (releases GIL).
    from concurrent.futures import ThreadPoolExecutor
    if progress_callback:
        progress_callback(10)
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [
            ex.submit(_fetch, video_fmt["url"], vid_path),
            ex.submit(_fetch, audio_fmt["url"], aud_path),
        ]
        for f in futs:
            f.result()
    if progress_callback:
        progress_callback(70)

    # Mux video + audio → original.mp4
    out_path = job_dir / "original.mp4"
    subprocess.run(
        [ffmpeg_path(), "-y", "-i", str(vid_path), "-i", str(aud_path),
         "-c", "copy", "-movflags", "+faststart", str(out_path)],
        check=True, capture_output=True, timeout=600,
    )

    # Cleanup intermediate files.
    for p in (vid_path, aud_path):
        try:
            p.unlink()
        except OSError:
            pass

    return {"title": title, "duration": duration}


def _download_via_cobalt(api_url: str, source_url: str, job_dir: Path,
                          progress_callback=None) -> dict:
    """POST URL to cobalt → get tunnel URL → stream download to job_dir.

    Returns metadata dict (title, duration). Raises on any failure so caller
    can fall back to yt-dlp.
    """
    import urllib.request
    import urllib.error

    api_url = api_url.rstrip("/")
    req_body = json.dumps({"url": source_url, "videoQuality": "1080"}).encode("utf-8")
    req = urllib.request.Request(
        api_url + "/",
        data=req_body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    status = data.get("status")
    if status not in ("tunnel", "redirect"):
        raise RuntimeError(f"cobalt status={status}: {data.get('error', {}).get('code', 'unknown')}")

    tunnel_url = data["url"]
    filename = data.get("filename", "original.mp4")
    ext = Path(filename).suffix.lstrip(".") or "mp4"
    out_path = job_dir / f"original.{ext}"

    with urllib.request.urlopen(tunnel_url, timeout=600) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0 and progress_callback:
                    progress_callback(int(downloaded / total * 80))

    return {"title": Path(filename).stem, "duration": 0}


def _has_audio_stream(video_path: str) -> bool:
    """ffprobe-based check: does the file contain at least one audio stream?"""
    try:
        result = subprocess.run(
            [ffprobe_path(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return "audio" in result.stdout
    except Exception:
        # Fail open — let downstream raise the real error rather than blocking
        # on a probe glitch.
        return True


def _needs_wav_extraction() -> bool:
    provider = os.getenv("TRANSCRIPTION_PROVIDER", "local").lower()
    # AssemblyAI ingests source video directly. Everything else needs a WAV.
    return provider != "assemblyai"


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

    # First pass: tolerant decode. `-err_detect ignore_err` skips malformed
    # AAC packets (e.g. HE-AAC v2 with 8 SBR bands — ffmpeg native decoder
    # caps at 7 and bails with exit 234). `-fflags +discardcorrupt` drops the
    # corrupt frames instead of aborting the whole stream.
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

    # Fallback: re-encode through aac_fixed (fixed-point AAC decoder, often
    # handles HE-AAC streams the LC decoder rejects).
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
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"].get("duration", 0))
