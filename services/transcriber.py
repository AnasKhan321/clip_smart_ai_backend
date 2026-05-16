import os
import json
import atexit
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path


_model_cache = {}

# Background pre-submission: if pipeline kicks off transcription early
# (e.g. as soon as R2 source key is known), it stores the in-flight Future
# here so the eventual `transcribe()` call in stage 2 just awaits the
# already-running job instead of submitting a duplicate request.
_inflight: dict = {}
# RLock (reentrant) — submit_async holds this while calling
# _get_inflight_executor(), which also acquires it. A plain Lock would deadlock.
_inflight_lock = threading.RLock()
_inflight_executor: ThreadPoolExecutor = None


def _get_inflight_executor() -> ThreadPoolExecutor:
    global _inflight_executor
    if _inflight_executor is None:
        with _inflight_lock:
            if _inflight_executor is None:
                _inflight_executor = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="transcribe-async"
                )
                # Ensure clean shutdown on worker exit (Railway redeploy etc.)
                atexit.register(_inflight_executor.shutdown, wait=False)
    return _inflight_executor


def _provider() -> str:
    return os.getenv("TRANSCRIPTION_PROVIDER", "local").lower()


# ── R2 mirror helpers (persist transcript/diarization across worker restarts) ─

def _r2_artifact_key(job_id: str, filename: str) -> str:
    return f"jobs/{job_id}/{filename}"


def _ensure_local_artifact(job_id: str, filename: str) -> bool:
    """If `{job_dir}/{filename}` missing locally but present in R2, pull it
    back to local disk so callers can read it. Returns True if file now
    exists locally.
    """
    storage = os.getenv("STORAGE_PATH", "./storage")
    local = Path(storage) / "jobs" / job_id / filename
    if local.exists():
        return True
    try:
        from services import r2 as _r2
        if not _r2.is_enabled():
            return False
        key = _r2_artifact_key(job_id, filename)
        if not _r2.object_exists(key):
            return False
        local.parent.mkdir(parents=True, exist_ok=True)
        _r2.download_file(key, str(local))
        return local.exists()
    except Exception as exc:
        print(f"[transcriber] r2 restore failed for {filename} ({exc})", flush=True)
        return False


def _mirror_artifact_to_r2(job_id: str, filename: str) -> None:
    """Background upload of small JSON artifact to R2. Fire-and-forget."""
    storage = os.getenv("STORAGE_PATH", "./storage")
    local = Path(storage) / "jobs" / job_id / filename
    if not local.exists():
        return
    try:
        from services import r2 as _r2
        if not _r2.is_enabled():
            return
        key = _r2_artifact_key(job_id, filename)
        _r2.upload_in_background(str(local), key, content_type="application/json")
    except Exception as exc:
        print(f"[transcriber] r2 mirror failed for {filename} ({exc})", flush=True)


def submit_async(job_id: str, language: str = None) -> Future:
    """Kick off transcription in background. Idempotent — safe to call
    multiple times, returns the same Future. Returns None if disk cache
    already satisfies the request.
    """
    storage = os.getenv("STORAGE_PATH", "./storage")
    if (Path(storage) / "jobs" / job_id / "transcript.json").exists():
        return None
    with _inflight_lock:
        existing = _inflight.get(job_id)
        if existing is not None:
            return existing
        fut = _get_inflight_executor().submit(_run_transcribe, job_id, language)
        _inflight[job_id] = fut
        return fut


def _run_transcribe(job_id: str, language: str = None) -> dict:
    p = _provider()
    if p == "assemblyai":
        return _transcribe_assemblyai(job_id, language, None)
    elif p == "openai":
        return _transcribe_openai(job_id, language, None)
    else:
        return _transcribe_local(job_id, language, None)


def transcribe(job_id: str, language: str = None, progress_callback=None) -> dict:
    storage = os.getenv("STORAGE_PATH", "./storage")
    transcript_path = Path(storage) / "jobs" / job_id / "transcript.json"
    # Restore from R2 if local disk was wiped (worker restart / redeploy).
    if not transcript_path.exists():
        _ensure_local_artifact(job_id, "transcript.json")
        _ensure_local_artifact(job_id, "diarization.json")
    if transcript_path.exists():
        if progress_callback:
            progress_callback(100)
        return load_transcript(job_id)

    # If a background pre-submission is in flight, wait on it instead of
    # firing a duplicate request to the provider. Poll with timeout so we
    # can issue periodic progress callbacks (keeps the DB session alive on
    # long jobs — Supabase session pooler idles out around 10 min).
    from concurrent.futures import TimeoutError as _FutTimeout
    with _inflight_lock:
        fut = _inflight.get(job_id)
    if fut is not None:
        try:
            pct = 10
            while True:
                try:
                    result = fut.result(timeout=60)
                    break
                except _FutTimeout:
                    if progress_callback:
                        # Bump 10 → 90 over time so the bar moves; pings DB.
                        pct = min(pct + 5, 90)
                        try:
                            progress_callback(pct)
                        except Exception:
                            pass
        finally:
            with _inflight_lock:
                _inflight.pop(job_id, None)
        if progress_callback:
            progress_callback(100)
        return result

    p = _provider()
    if p == "assemblyai":
        return _transcribe_assemblyai(job_id, language, progress_callback)
    elif p == "openai":
        return _transcribe_openai(job_id, language, progress_callback)
    else:
        return _transcribe_local(job_id, language, progress_callback)


# ── Local (whisper) ──────────────────────────────────────────────────────────

def _transcribe_local(job_id: str, language: str = None, progress_callback=None) -> dict:
    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError(
            "TRANSCRIPTION_PROVIDER=local requires openai-whisper. "
            "Install it with `pip install openai-whisper` or set TRANSCRIPTION_PROVIDER=assemblyai/openai."
        ) from exc

    storage = os.getenv("STORAGE_PATH", "./storage")
    audio_path = Path(storage) / "jobs" / job_id / "audio.wav"
    transcript_path = Path(storage) / "jobs" / job_id / "transcript.json"

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if progress_callback:
        progress_callback(10)

    if "large-v3" not in _model_cache:
        _model_cache["large-v3"] = whisper.load_model("large-v3")
    model = _model_cache["large-v3"]

    if progress_callback:
        progress_callback(30)

    kwargs = {"word_timestamps": True, "verbose": False, "task": "transcribe"}
    if language:
        kwargs["language"] = language

    result = model.transcribe(str(audio_path), **kwargs)

    # Indian-accented English often misdetected as hi/ur. Re-run forcing English.
    if not language and _is_likely_misdetected_english(result):
        kwargs["language"] = "en"
        result = model.transcribe(str(audio_path), **kwargs)
        result["language"] = "en"
        result["_language_corrected"] = True

    _check_transcript_quality(result)

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    _mirror_artifact_to_r2(job_id, "transcript.json")

    if progress_callback:
        progress_callback(100)

    return result


# ── OpenAI Whisper API ───────────────────────────────────────────────────────

def _transcribe_openai(job_id: str, language: str = None, progress_callback=None) -> dict:
    from openai import OpenAI

    storage = os.getenv("STORAGE_PATH", "./storage")
    audio_path = Path(storage) / "jobs" / job_id / "audio.wav"
    transcript_path = Path(storage) / "jobs" / job_id / "transcript.json"

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if progress_callback:
        progress_callback(10)

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    kwargs = {
        "model": "whisper-1",
        "response_format": "verbose_json",
        "timestamp_granularities": ["word", "segment"],
    }
    if language:
        kwargs["language"] = language

    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(file=f, **kwargs)

    if progress_callback:
        progress_callback(70)

    all_words = response.words or []
    segments = []
    for seg in (response.segments or []):
        seg_words = [
            w for w in all_words
            if w.start >= seg.start and w.end <= seg.end + 0.1
        ]
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
            "words": [
                {"word": w.word, "start": w.start, "end": w.end, "probability": 1.0}
                for w in seg_words
            ],
        })

    result = {"language": response.language or "unknown", "segments": segments}

    if not language and _is_likely_misdetected_english(result):
        kwargs["language"] = "en"
        with open(audio_path, "rb") as f:
            response = client.audio.transcriptions.create(file=f, **kwargs)
        all_words = response.words or []
        segments = []
        for seg in (response.segments or []):
            seg_words = [
                w for w in all_words
                if w.start >= seg.start and w.end <= seg.end + 0.1
            ]
            segments.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "words": [
                    {"word": w.word, "start": w.start, "end": w.end, "probability": 1.0}
                    for w in seg_words
                ],
            })
        result = {"language": "en", "segments": segments, "_language_corrected": True}

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    _mirror_artifact_to_r2(job_id, "transcript.json")

    if progress_callback:
        progress_callback(100)

    return result


# ── AssemblyAI ───────────────────────────────────────────────────────────────

def _transcribe_assemblyai(job_id: str, language: str = None, progress_callback=None) -> dict:
    try:
        import assemblyai as aai
    except ImportError as exc:
        raise RuntimeError(
            "TRANSCRIPTION_PROVIDER=assemblyai requires the assemblyai package. "
            "Install it with `pip install assemblyai` or rebuild the Docker image after updating requirements.txt."
        ) from exc

    storage = os.getenv("STORAGE_PATH", "./storage")
    job_dir = Path(storage) / "jobs" / job_id
    transcript_path = job_dir / "transcript.json"
    diarization_path = job_dir / "diarization.json"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Prefer giving AssemblyAI a direct R2 URL — AAI fetches the source
    # itself, skipping our local pull + ffmpeg compact extract + POST upload.
    # Falls back to the local upload path when R2 isn't ready (e.g. URL flow
    # where the background mirror hasn't completed yet).
    upload_target = None
    try:
        from services import r2 as _r2
        if _r2.is_enabled():
            r2_key = _r2.source_key(job_id)
            if _r2.object_exists(r2_key):
                # 2hr TTL so long AssemblyAI queue + 2hr podcast fetch can
                # complete before the URL expires.
                upload_target = _r2.object_url(r2_key, ttl=7200)
    except Exception as exc:
        print(f"[transcriber] r2 url lookup failed ({exc}), using local upload", flush=True)
        upload_target = None

    if upload_target is None:
        # Local-upload fallback: find source media (mp4/mkv/webm/m4a/wav).
        source_path = job_dir / "original.mp4"
        if not source_path.exists():
            for ext in ("mkv", "webm", "m4a"):
                cand = job_dir / f"original.{ext}"
                if cand.exists():
                    source_path = cand
                    break
            else:
                audio_fallback = job_dir / "audio.wav"
                if audio_fallback.exists():
                    source_path = audio_fallback
                else:
                    raise FileNotFoundError(
                        f"No source media for transcription in {job_dir}"
                    )

        # Extract a compact m4a (AAC mono 16kHz 32kbps) for the upload. A 1hr
        # video shrinks from ~1GB → ~15MB, making the Railway→AssemblyAI POST
        # reliable. Skip if source is already small audio.
        upload_path = source_path
        if source_path.suffix.lower() in (".mp4", ".mkv", ".webm"):
            import subprocess as _sp
            from services.media_tools import ffmpeg_path as _ffmpeg
            compact = job_dir / "upload_audio.m4a"
            try:
                _sp.run(
                    [_ffmpeg(), "-y", "-i", str(source_path), "-vn",
                     "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k",
                     str(compact)],
                    check=True, capture_output=True, timeout=600,
                )
                if compact.exists() and compact.stat().st_size > 1024:
                    upload_path = compact
            except Exception as exc:
                print(f"[transcriber] compact audio extract failed ({exc}), uploading source", flush=True)
        upload_target = str(upload_path)

    api_key = os.getenv("ASSEMBLYAI_API_KEY")
    if not api_key:
        raise RuntimeError("ASSEMBLYAI_API_KEY is required when TRANSCRIPTION_PROVIDER=assemblyai")
    aai.settings.api_key = api_key

    if progress_callback:
        progress_callback(10)

    cfg_kwargs = {
        "speaker_labels": True,
        "speech_models": ["universal-2"],
    }
    if language:
        cfg_kwargs["language_code"] = language

    config = aai.TranscriptionConfig(**cfg_kwargs)
    result = aai.Transcriber().transcribe(upload_target, config=config)

    if result.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI error: {result.error}")

    if progress_callback:
        progress_callback(80)

    # Build segments from utterances (include speaker-word timestamps)
    segments = []
    for utt in (result.utterances or []):
        seg_words = [
            {
                "word": w.text,
                "start": w.start / 1000.0,
                "end": w.end / 1000.0,
                "probability": w.confidence or 1.0,
            }
            for w in (utt.words or [])
        ]
        segments.append({
            "start": utt.start / 1000.0,
            "end": utt.end / 1000.0,
            "text": utt.text.strip(),
            "words": seg_words,
        })

    # Fallback: group raw words into ~10s buckets if no utterances
    if not segments and result.words:
        current: dict = {}
        for w in result.words:
            ws, we = w.start / 1000.0, w.end / 1000.0
            if not current or ws - current["start"] > 10:
                current = {"start": ws, "end": we, "text": "", "words": []}
                segments.append(current)
            current["end"] = we
            current["text"] = (current["text"] + " " + w.text).strip()
            current["words"].append(
                {"word": w.text, "start": ws, "end": we, "probability": w.confidence or 1.0}
            )

    transcript_data = {
        "language": result.language_code or "unknown",
        "segments": segments,
    }

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(transcript_data, f, ensure_ascii=False, indent=2)

    # Save diarization so diarizer.py can load it as usual
    diarization = [
        {"speaker": utt.speaker, "start": round(utt.start / 1000.0, 3), "end": round(utt.end / 1000.0, 3)}
        for utt in (result.utterances or [])
    ]
    with open(diarization_path, "w") as f:
        json.dump(diarization, f, indent=2)

    _mirror_artifact_to_r2(job_id, "transcript.json")
    _mirror_artifact_to_r2(job_id, "diarization.json")

    if progress_callback:
        progress_callback(100)

    return transcript_data


# ── Shared helpers ───────────────────────────────────────────────────────────

_LATIN_FALLBACK_LANGS = {"hi", "ur", "mr", "ne", "bn", "pa", "gu", "ta", "te", "kn", "ml", "sa"}


def _is_likely_misdetected_english(result: dict) -> bool:
    """Whisper often labels Indian-accented English as hi/ur. If detected lang is one of those
    but transcript text is overwhelmingly ASCII/Latin, treat as misdetection."""
    lang = (result.get("language") or "").lower()
    if lang not in _LATIN_FALLBACK_LANGS:
        return False
    text = " ".join((seg.get("text") or "") for seg in result.get("segments", []))
    text = text.strip()
    if len(text) < 20:
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    latin = sum(1 for c in letters if ord(c) < 128)
    return (latin / len(letters)) > 0.85


def _check_transcript_quality(result: dict):
    segments = result.get("segments", [])
    if not segments:
        return
    total = low = 0
    for seg in segments:
        for word in seg.get("words", []):
            total += 1
            if word.get("probability", 1.0) < 0.5:
                low += 1
    if total > 0 and low / total > 0.4:
        result["_quality_warning"] = (
            "High proportion of low-confidence words detected. "
            "This may be music or non-speech content."
        )


_transcript_cache: dict = {}
_TRANSCRIPT_CACHE_MAX = 4
# Guards _transcript_cache. Threaded Celery pool can hit this concurrently.
_transcript_cache_lock = threading.Lock()


def load_transcript(job_id: str) -> dict:
    with _transcript_cache_lock:
        cached = _transcript_cache.get(job_id)
    if cached is not None:
        return cached
    storage = os.getenv("STORAGE_PATH", "./storage")
    path = Path(storage) / "jobs" / job_id / "transcript.json"
    if not path.exists():
        _ensure_local_artifact(job_id, "transcript.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    with _transcript_cache_lock:
        if len(_transcript_cache) >= _TRANSCRIPT_CACHE_MAX:
            # Drop oldest insertion (CPython dicts preserve order)
            _transcript_cache.pop(next(iter(_transcript_cache)))
        _transcript_cache[job_id] = data
    return data


def invalidate_transcript_cache(job_id: str = None):
    with _transcript_cache_lock:
        if job_id is None:
            _transcript_cache.clear()
        else:
            _transcript_cache.pop(job_id, None)
