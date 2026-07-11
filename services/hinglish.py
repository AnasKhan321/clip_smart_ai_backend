"""Romanize Devanagari transcript text to casual Hinglish for captions.

Whisper/AssemblyAI/etc return Hindi speech in Devanagari script. Captions
should read the way people actually type Hindi on social media — Roman
letters, no diacritics, e.g. "क्या हाल है" -> "kya haal hai" — not formal
IAST/ITRANS transliteration and not an English translation.
"""
from __future__ import annotations

import json
import logging
import re

from services.llm_provider import generate_text, model_for

logger = logging.getLogger(__name__)

DEVANAGARI_RUN = re.compile(r"[ऀ-ॿ]+")
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"


def _has_devanagari(text: str) -> bool:
    return bool(DEVANAGARI_RUN.search(text or ""))


def _collect_unique_words(transcript: dict) -> list[str]:
    seen: dict[str, None] = {}
    for seg in transcript.get("segments", []):
        for run in DEVANAGARI_RUN.findall(seg.get("text", "")):
            seen.setdefault(run, None)
        for w in seg.get("words", []):
            for run in DEVANAGARI_RUN.findall(w.get("word", "")):
                seen.setdefault(run, None)
    return list(seen.keys())


def _build_mapping(words: list[str]) -> dict[str, str]:
    prompt = (
        "Transliterate each Hindi word below into casual Hinglish — Roman "
        "script the way young Indians type Hindi on social media (e.g. "
        '"क्या" -> "kya", "है" -> "hai", "नहीं" -> "nahi"). '
        "Plain Roman letters only, NO diacritics/IAST marks (no ā, ī, ś etc). "
        "This is romanization, not English translation — keep the same words, "
        "just in Latin script.\n\n"
        "Return ONLY a JSON object mapping each input word to its Hinglish "
        "spelling, one entry per word, same words as keys:\n\n"
        + json.dumps(words, ensure_ascii=False)
    )
    model = model_for("json", openrouter_default=DEFAULT_MODEL, gemini_default="gemini-3.1-flash-lite")
    raw = generate_text(
        prompt,
        purpose="json",
        openrouter_model=model,
        gemini_model=model,
        max_tokens=4096,
        json_response=True,
    ).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw).strip()
    mapping = json.loads(raw)
    if not isinstance(mapping, dict):
        raise ValueError("expected a JSON object mapping")
    return {str(k): str(v) for k, v in mapping.items()}


def _apply_mapping(transcript: dict, mapping: dict[str, str]) -> None:
    def sub(text: str) -> str:
        return DEVANAGARI_RUN.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)

    for seg in transcript.get("segments", []):
        if "text" in seg:
            seg["text"] = sub(seg["text"])
        for w in seg.get("words", []):
            if "word" in w:
                w["word"] = sub(w["word"])


def ensure_hinglish(transcript: dict) -> bool:
    """Mutates transcript in place, romanizing any Devanagari text found.
    Returns True if a change was made (caller should re-persist to disk)."""
    if transcript.get("_hinglish_applied"):
        return False
    words = _collect_unique_words(transcript)
    if not words:
        transcript["_hinglish_applied"] = True
        return False
    try:
        mapping = _build_mapping(words)
    except Exception:
        logger.exception("hinglish transliteration LLM call failed — leaving Devanagari as-is")
        return False
    _apply_mapping(transcript, mapping)
    transcript["_hinglish_applied"] = True
    return True
