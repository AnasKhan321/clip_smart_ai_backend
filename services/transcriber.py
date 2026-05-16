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
    elif p == "groq":
        return _transcribe_groq(job_id, language, None)
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
    elif p == "groq":
        return _transcribe_groq(job_id, language, progress_callback)
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


# ── Groq (Whisper-large-v3 on LPU — fastest on market) ──────────────────────

_GROQ_MAX_FILE_BYTES = 24 * 1024 * 1024  # 24MB safe limit (API max is 25MB)
_GROQ_CHUNK_DURATION = 600               # 10 min chunks when splitting


def _transcribe_groq(job_id: str, language: str = None, progress_callback=None) -> dict:
    """Transcribe via Groq's Whisper-large-v3 endpoint.

    Extremely fast (~10–30s for hours of audio) at $0.04/hr.
    No diarization — the LLM analyzer infers speakers from text.

    If the compact audio exceeds 25MB (long podcasts), it's automatically
    split into chunks, each chunk is transcribed in parallel, and results
    are merged with proper time offsets.
    """
    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError(
            "TRANSCRIPTION_PROVIDER=groq requires the groq package. "
            "Install with `pip install groq`."
        ) from exc

    storage = os.getenv("STORAGE_PATH", "./storage")
    job_dir = Path(storage) / "jobs" / job_id
    transcript_path = job_dir / "transcript.json"
    job_dir.mkdir(parents=True, exist_ok=True)

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is required when TRANSCRIPTION_PROVIDER=groq")

    if progress_callback:
        progress_callback(5)

    # ── Step 1: extract compact audio (mono 16kHz 48kbps) ────────────────
    compact_path = _groq_extract_compact_audio(job_dir)
    file_size = compact_path.stat().st_size

    if progress_callback:
        progress_callback(10)

    # ── Step 2: transcribe (single-shot or chunked) ──────────────────────
    client = Groq(api_key=api_key)
    model = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")

    if file_size <= _GROQ_MAX_FILE_BYTES:
        result = _groq_transcribe_single(client, model, compact_path, language)
    else:
        result = _groq_transcribe_chunked(
            client, model, compact_path, language, job_dir, progress_callback,
        )

    # Indian-accent English misdetection fix (same as other providers)
    if not language and _is_likely_misdetected_english(result):
        if file_size <= _GROQ_MAX_FILE_BYTES:
            result = _groq_transcribe_single(client, model, compact_path, "en")
        else:
            result = _groq_transcribe_chunked(
                client, model, compact_path, "en", job_dir, progress_callback,
            )
        result["language"] = "en"
        result["_language_corrected"] = True

    _check_transcript_quality(result)

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    _mirror_artifact_to_r2(job_id, "transcript.json")

    if progress_callback:
        progress_callback(100)

    return result


def _groq_extract_compact_audio(job_dir: Path) -> Path:
    """Extract compact m4a from source video. Returns path to audio file."""
    # Check if we already have compact audio
    compact = job_dir / "groq_audio.m4a"
    if compact.exists() and compact.stat().st_size > 1024:
        return compact

    # Find source media
    source_path = job_dir / "original.mp4"
    if not source_path.exists():
        for ext in ("mkv", "webm", "m4a"):
            cand = job_dir / f"original.{ext}"
            if cand.exists():
                source_path = cand
                break
        else:
            audio_wav = job_dir / "audio.wav"
            if audio_wav.exists():
                source_path = audio_wav
            else:
                raise FileNotFoundError(f"No source media in {job_dir}")

    # If source is already small audio, use it directly
    if source_path.suffix.lower() in (".m4a", ".mp3", ".ogg"):
        if source_path.stat().st_size <= _GROQ_MAX_FILE_BYTES:
            return source_path

    # Extract compact audio: mono, 16kHz, 48kbps AAC
    # 48kbps (vs 32k for AssemblyAI) gives ~21MB for 1hr — stays under 25MB
    # while preserving enough quality for Whisper accuracy.
    import subprocess as _sp
    from services.media_tools import ffmpeg_path as _ffmpeg
    try:
        _sp.run(
            [_ffmpeg(), "-y", "-i", str(source_path), "-vn",
             "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k",
             str(compact)],
            check=True, capture_output=True, timeout=600,
        )
    except Exception as exc:
        raise RuntimeError(f"Audio extraction for Groq failed: {exc}") from exc

    if not compact.exists() or compact.stat().st_size < 1024:
        raise RuntimeError("Audio extraction produced empty file")

    return compact


def _groq_transcribe_single(client, model: str, audio_path: Path,
                            language: str = None) -> dict:
    """Transcribe a single file under 25MB."""
    kwargs = {
        "model": model,
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment", "word"],
    }
    if language:
        kwargs["language"] = language

    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(
            file=(audio_path.name, f.read()),
            **kwargs,
        )

    return _groq_response_to_dict(response)


def _groq_transcribe_chunked(client, model: str, audio_path: Path,
                             language: str, job_dir: Path,
                             progress_callback=None) -> dict:
    """Split audio into chunks, transcribe each, merge with time offsets."""
    import subprocess as _sp
    from services.media_tools import ffmpeg_path as _ffmpeg, ffprobe_path as _ffprobe

    # Get total duration
    try:
        probe = _sp.run(
            [_ffprobe(), "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(audio_path)],
            capture_output=True, text=True, timeout=30,
        )
        total_duration = float(probe.stdout.strip())
    except Exception:
        total_duration = 7200  # fallback 2hr

    # Split into chunks
    chunk_dir = job_dir / "groq_chunks"
    chunk_dir.mkdir(exist_ok=True)
    chunks = []
    offset = 0.0
    chunk_idx = 0

    while offset < total_duration:
        chunk_file = chunk_dir / f"chunk_{chunk_idx:03d}.m4a"
        if not chunk_file.exists():
            _sp.run(
                [_ffmpeg(), "-y", "-i", str(audio_path),
                 "-ss", str(offset), "-t", str(_GROQ_CHUNK_DURATION),
                 "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-ar", "16000",
                 str(chunk_file)],
                check=True, capture_output=True, timeout=120,
            )
        chunks.append((chunk_file, offset))
        offset += _GROQ_CHUNK_DURATION
        chunk_idx += 1

    # Transcribe all chunks in parallel — brings ~175s down to ~20-30s
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    import time as _time

    def _transcribe_one_chunk(chunk_file, lang):
        """Transcribe a single chunk with retry logic."""
        kwargs = {
            "model": model,
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment", "word"],
        }
        if lang:
            kwargs["language"] = lang
        for attempt in range(1, 4):
            try:
                with open(chunk_file, "rb") as f:
                    resp = client.audio.transcriptions.create(
                        file=(chunk_file.name, f.read()),
                        **kwargs,
                    )
                return _groq_response_to_dict(resp)
            except Exception as exc:
                if attempt < 3:
                    _time.sleep(2 ** attempt)
                else:
                    raise RuntimeError(
                        f"Groq failed on {chunk_file.name} after 3 attempts: {exc}"
                    ) from exc

    max_workers = min(len(chunks), 6)  # Cap at 6 to avoid Groq rate limits
    results = [None] * len(chunks)
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(_transcribe_one_chunk, cf, language): i
            for i, (cf, _off) in enumerate(chunks)
        }
        for fut in _as_completed(future_to_idx):
            idx = future_to_idx[fut]
            results[idx] = fut.result()
            completed += 1
            if progress_callback:
                pct = 10 + int(completed / len(chunks) * 85)
                progress_callback(pct)

    # Merge results in order with proper time offsets
    all_segments = []
    detected_lang = "unknown"

    for i, ((_cf, time_offset), chunk_result) in enumerate(zip(chunks, results)):
        if i == 0:
            detected_lang = chunk_result.get("language", "unknown")
        for seg in chunk_result.get("segments", []):
            seg["start"] += time_offset
            seg["end"] += time_offset
            for w in seg.get("words", []):
                w["start"] += time_offset
                w["end"] += time_offset
            all_segments.append(seg)

    # Cleanup chunk files
    import shutil
    shutil.rmtree(chunk_dir, ignore_errors=True)

    return {"language": detected_lang, "segments": all_segments}


def _get_val(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)

def _groq_response_to_dict(response) -> dict:
    """Convert Groq transcription response to our standard format."""
    all_words = _get_val(response, "words") or []
    raw_segments = _get_val(response, "segments") or []
    segments = []

    for seg in raw_segments:
        s_start = _get_val(seg, "start", 0.0)
        s_end = _get_val(seg, "end", 0.0)
        s_text = _get_val(seg, "text", "")
        # Match words to segment by time range
        seg_words = [
            w for w in all_words
            if _get_val(w, "start", 0.0) >= s_start and _get_val(w, "end", 0.0) <= s_end + 0.1
        ]
        segments.append({
            "start": s_start,
            "end": s_end,
            "text": s_text.strip(),
            "words": [
                {
                    "word": _get_val(w, "word", ""),
                    "start": _get_val(w, "start", 0.0),
                    "end": _get_val(w, "end", 0.0),
                    "probability": 1.0
                }
                for w in seg_words
            ],
        })

    # Fallback: build segments from words if no segment metadata
    if not segments and all_words:
        current = None
        for w in all_words:
            w_start = _get_val(w, "start", 0.0)
            w_end = _get_val(w, "end", 0.0)
            w_word = _get_val(w, "word", "")
            if current is None or w_start - current["start"] > 10:
                current = {"start": w_start, "end": w_end, "text": "", "words": []}
                segments.append(current)
            current["end"] = w_end
            current["text"] = (current["text"] + " " + w_word).strip()
            current["words"].append(
                {"word": w_word, "start": w_start, "end": w_end, "probability": 1.0}
            )

    lang = _get_val(response, "language", "unknown")
    if not lang:
        lang = "unknown"

    return {
        "language": lang,
        "segments": segments,
    }


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
    #
    # IMPORTANT: we upload the COMPACT AUDIO (mono 16kHz 32kbps m4a), not the
    # full video. A 2hr 1080p video is ~1-2GB; the compact audio is ~28MB.
    # Giving AAI the full video R2 URL caused 30+ min transcription times
    # because AAI had to download the entire video before extracting audio.
    upload_target = None

    # Always extract compact audio first — ~30s locally, saves 20+ min on AAI
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

    upload_path = source_path
    if source_path.suffix.lower() in (".mp4", ".mkv", ".webm", ".wav"):
        import subprocess as _sp
        from services.media_tools import ffmpeg_path as _ffmpeg
        compact = job_dir / "upload_audio.m4a"
        if not compact.exists() or compact.stat().st_size < 1024:
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
        else:
            upload_path = compact

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
