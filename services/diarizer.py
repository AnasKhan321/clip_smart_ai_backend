import os
import json
from pathlib import Path


def _provider() -> str:
    return os.getenv("TRANSCRIPTION_PROVIDER", "local").lower()


def diarize(job_id: str, progress_callback=None) -> list:
    # Skip if already done (saves credits on retry)
    existing = load_diarization(job_id)
    if existing:
        if progress_callback:
            progress_callback(100)
        return existing

    p = _provider()

    if p == "assemblyai":
        # Transcriber already saved diarization.json during transcription
        if progress_callback:
            progress_callback(100)
        return load_diarization(job_id)

    if p == "openai":
        # OpenAI Whisper API has no diarization
        if progress_callback:
            progress_callback(100)
        return []

    return _diarize_local(job_id, progress_callback)


def _diarize_local(job_id: str, progress_callback=None) -> list:
    from pyannote.audio import Pipeline
    import torch

    storage = os.getenv("STORAGE_PATH", "./storage")
    audio_path = Path(storage) / "jobs" / job_id / "audio.wav"
    diarization_path = Path(storage) / "jobs" / job_id / "diarization.json"
    hf_token = os.getenv("HF_TOKEN")

    if not hf_token:
        raise ValueError("HF_TOKEN not set")

    if progress_callback:
        progress_callback(10)

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    pipeline = pipeline.to(torch.device(device))

    if progress_callback:
        progress_callback(30)

    diarization = pipeline(str(audio_path))

    segments = [
        {"speaker": speaker, "start": round(turn.start, 3), "end": round(turn.end, 3)}
        for turn, _, speaker in diarization.itertracks(yield_label=True)
    ]

    with open(diarization_path, "w") as f:
        json.dump(segments, f, indent=2)

    if progress_callback:
        progress_callback(100)

    return segments


def load_diarization(job_id: str) -> list:
    storage = os.getenv("STORAGE_PATH", "./storage")
    path = Path(storage) / "jobs" / job_id / "diarization.json"
    if not path.exists():
        return []
    with open(path, "r") as f:
        return json.load(f)


def get_speaker_at(segments: list, time_seconds: float) -> str:
    for seg in segments:
        if seg["start"] <= time_seconds <= seg["end"]:
            return seg["speaker"]
    return "UNKNOWN"


def build_enriched_transcript(transcript: dict, diarization: list) -> list:
    enriched = []
    for seg in transcript.get("segments", []):
        mid = (seg["start"] + seg["end"]) / 2
        speaker = get_speaker_at(diarization, mid)
        enriched.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"].strip(),
            "speaker": speaker,
            "words": seg.get("words", []),
        })
    return enriched
