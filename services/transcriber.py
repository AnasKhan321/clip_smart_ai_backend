import os
import json
from pathlib import Path


_model_cache = {}


def _provider() -> str:
    return os.getenv("TRANSCRIPTION_PROVIDER", "local").lower()


def transcribe(job_id: str, language: str = None, progress_callback=None) -> dict:
    storage = os.getenv("STORAGE_PATH", "./storage")
    transcript_path = Path(storage) / "jobs" / job_id / "transcript.json"
    if transcript_path.exists():
        if progress_callback:
            progress_callback(100)
        return load_transcript(job_id)

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
    audio_path = Path(storage) / "jobs" / job_id / "audio.wav"
    transcript_path = Path(storage) / "jobs" / job_id / "transcript.json"
    diarization_path = Path(storage) / "jobs" / job_id / "diarization.json"

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

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
    result = aai.Transcriber().transcribe(str(audio_path), config=config)

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


def load_transcript(job_id: str) -> dict:
    storage = os.getenv("STORAGE_PATH", "./storage")
    path = Path(storage) / "jobs" / job_id / "transcript.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
