"""Turn freeform pasted timestamps into clean skip ranges.

Users paste whatever they have — YouTube chapter dumps, sponsor-block notes,
half-typed ranges, "skip the first 2 min". The regex in analyzer.py only
handles well-formed `M:SS-M:SS` pairs, so anything else silently vanishes.
This runs the text through the LLM, falls back to the regex if that fails.
"""
import json
import logging
import os
import re

from services.analyzer import parse_timestamp_ranges
from services.llm_provider import generate_text, model_for

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 4000
_DEFAULT_SPAN = 30.0  # assumed length when the user gave only one timestamp

_PROMPT = """You clean up messy user-pasted video timestamps into skip ranges.

Video duration: {duration}
User text:
---
{text}
---

Return ONLY a JSON object:
{{"ranges": [{{"source": "sponsor 1:20-2:45", "start": "1:20", "end": "2:45", "label": "sponsor", "note": null, "needs_review": false}}],
  "warnings": ["..."]}}

Rules:
- Work through the user's entries one at a time, in the order written. Emit one range per entry before moving to the next. Never carry a label, note or time from one entry to another.
- "source" is the user's own text for that entry, copied exactly. Every other field must be derived from that same source string and nothing else.
- "start"/"end" are timestamp strings: "M:SS", "H:MM:SS", or a plain number of seconds. Copy the digits the user wrote — do NOT convert to seconds, do NOT do arithmetic, do NOT round. "3:10" stays "3:10".
- Accept any input format: "1:20-2:45", "01:10:00 to 01:12:30", "2m10s", "90", "1.30 – 2.00", chapter lists, bullet lists, prose. Rewrite odd ones into the shapes above: "2m10s" -> "2:10", "3m" -> "3:00", "45s" -> "45", "1.30" -> "1:30", "1h02m" -> "1:02:00".
- A bare number with no colon is seconds.
- If you can read a time out of an entry at all, you MUST emit a range for it. A warning is never a substitute for a range — warn AND emit.
- "0:00" is a valid, ordinary start time meaning the very beginning of the video. "0:00-2:00" is a normal entry: start "0:00", end "2:00". Never treat a 0:00 entry as unparseable.
- Only one endpoint given ("skip 4:10", "sponsor at 2:00", "from 5:00"): use it as "start", set "end" to null, set needs_review true, and say in note that only a start was given.
- "first N minutes" -> start "0:00", end "N:00".
- "last N minutes" -> start is the video duration minus N minutes, end is the video duration. Both written as timestamps, e.g. duration 30:00 and "last 2 minutes" -> start "28:00", end "30:00". If the duration is unknown, drop the entry and add a warning.
- Reversed pairs (end before start, e.g. "4:50 - 3:10"): keep the entry, swap the two so start is the earlier one, note it. Never drop it.
- Impossible values ("2:75", end past the video duration): keep the entry, set needs_review true, note it. Do not try to fix the number.
- Drop entries that carry no usable time at all, and say so in warnings.
- label is a 1-3 word tag from the user's own words, or null.
- warnings: short plain-English strings about anything you guessed, fixed, or dropped. Empty list if the input was clean.
"""


def _fmt_duration(duration: float | None) -> str:
    """As a timestamp, not just seconds — "last 2 minutes" then needs no math."""
    if not duration:
        return "unknown"
    total = int(duration)
    return f"{total // 60}:{total % 60:02d} ({total} seconds)"


def _to_seconds(v) -> float | None:
    """"3:10" / "1:00:00" / 90 / "90" -> seconds. None if unusable.
    The LLM hands back the digits the user typed; the arithmetic happens here,
    because LLMs quietly get M*60+S wrong and round to tidy numbers."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v).strip().split(":")
    if not parts or not all(p.strip().isdigit() for p in parts) or len(parts) > 3:
        return None
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.insert(0, 0)
    return float(nums[0] * 3600 + nums[1] * 60 + nums[2])


def _clean(ranges: list, duration: float | None) -> list:
    """Convert, fill in missing endpoints, clamp, merge, sort. The LLM is told
    to do none of this — it only reads the text."""
    out = []
    for r in ranges:
        start = _to_seconds(r.get("start"))
        end = _to_seconds(r.get("end"))
        if start is None and end is None:
            continue
        needs_review = bool(r.get("needs_review"))
        note = r.get("note") or None
        if start is None or end is None:  # one endpoint typed — assume a span
            anchor = start if start is not None else end
            start, end = anchor, anchor + _DEFAULT_SPAN
            needs_review = True
            note = note or f"Only one time was given — assumed {int(_DEFAULT_SPAN)}s."
        start = max(0.0, start)
        if end < start:
            start, end = end, start
        if duration and end > duration:
            end = duration
            if start >= duration:
                continue
            needs_review = True
            note = note or "Ran past the end of the video — trimmed."
        if end - start < 0.5:
            continue
        out.append({
            "start_seconds": start,
            "end_seconds": end,
            "label": (r.get("label") or None),
            "note": note,
            "needs_review": needs_review,
        })

    out.sort(key=lambda r: r["start_seconds"])
    merged: list = []
    for r in out:
        prev = merged[-1] if merged else None
        if prev and r["start_seconds"] <= prev["end_seconds"]:
            prev["end_seconds"] = max(prev["end_seconds"], r["end_seconds"])
            prev["needs_review"] = prev["needs_review"] or r["needs_review"]
            prev["note"] = prev["note"] or r["note"]
            prev["label"] = prev["label"] or r["label"]
        else:
            merged.append(r)
    return merged


def _well_formed_pairs(text: str) -> list:
    """Unambiguous "1:20-2:45" style pairs, in the shape _clean wants."""
    return [
        {"start": r["start_seconds"], "end": r["end_seconds"]}
        for r in parse_timestamp_ranges(text)
    ]


def _regex_fallback(text: str) -> list:
    """Well-formed pairs; if there are none, bare single timestamps.
    Emits the same shape the LLM does, so _clean handles both."""
    ranges = _well_formed_pairs(text)
    if ranges:
        return ranges
    return [
        {"start": m.group(1), "end": None}
        for m in re.finditer(r"\b(\d{1,2}(?::\d{2}){1,2})\b", text)
    ]


def parse_skip_text(text: str, duration: float | None = None) -> dict:
    text = (text or "").strip()[:MAX_INPUT_CHARS]
    if not text:
        return {"ranges": [], "warnings": []}

    prompt = _PROMPT.format(
        text=text,
        duration=_fmt_duration(duration),
        span=int(_DEFAULT_SPAN),
    )
    # ponytail: flash-lite was cheaper but kept silently dropping entries it
    # could plainly read; the input here is a few lines, so flash costs little.
    model = os.getenv("SKIP_PARSER_MODEL") or model_for(
        "json", openrouter_default="google/gemini-2.5-flash", gemini_default="gemini-2.5-flash")
    try:
        raw = generate_text(
            prompt,
            purpose="json",
            openrouter_model=model,
            gemini_model=model,
            max_tokens=2000,
            temperature=0,
            json_response=True,
        )
        body = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        data = json.loads(body)
        llm_ranges = data.get("ranges") or []
        warnings = [str(w) for w in (data.get("warnings") or [])][:6]
        # Safety net: small models sometimes call a plainly well-formed pair
        # unparseable (bare "0:00-2:00" is a repeat offender). Any pair the
        # regex is sure about goes in too; _clean merges the duplicates.
        ranges = _clean(llm_ranges + _well_formed_pairs(text), duration)
        if len(ranges) > len(_clean(llm_ranges, duration)):
            warnings = [w for w in warnings if "pars" not in w.lower()]
    except Exception as e:  # any LLM/parse failure -> regex, never a 500
        logger.warning("skip parse LLM failed (%s) — falling back to regex", e)
        ranges = _clean(_regex_fallback(text), duration)
        warnings = [] if ranges else ["Could not find any timestamps in that text."]

    if not ranges:
        # Same story as the safety net above: the model sometimes shrugs at text
        # that plainly holds a timestamp. Trust the regex over the shrug.
        ranges = _clean(_regex_fallback(text), duration)
        if ranges:
            warnings = [w for w in warnings if "pars" not in w.lower()]

    if not ranges and not warnings:
        warnings = ["Could not find any timestamps in that text."]
    return {"ranges": ranges, "warnings": warnings}


if __name__ == "__main__":  # ponytail: self-check, no LLM — covers _to_seconds/_clean/fallback
    assert _to_seconds("3:10") == 190.0 and _to_seconds("1:00:00") == 3600.0
    assert _to_seconds("90") == 90.0 and _to_seconds(90) == 90.0
    assert _to_seconds("banana") is None and _to_seconds(None) is None and _to_seconds("1:2:3:4") is None

    # reversed pair gets swapped, not dropped
    assert _clean([{"start": "4:50", "end": "3:10"}], None)[0]["start_seconds"] == 190.0
    # one endpoint -> assumed span, flagged
    solo = _clean([{"start": "2:00", "end": None}], None)[0]
    assert solo["end_seconds"] == 150.0 and solo["needs_review"]
    # overlapping ranges merge
    merged = _clean([{"start": 0, "end": 30}, {"start": 20, "end": 45}], None)
    assert len(merged) == 1 and merged[0]["end_seconds"] == 45.0
    # past the end of the video -> trimmed and flagged; fully past -> dropped
    past = _clean([{"start": 0, "end": 999}], 100.0)[0]
    assert past["end_seconds"] == 100.0 and past["needs_review"]
    assert _clean([{"start": 200, "end": 300}], 100.0) == []
    assert _clean([{"start": "x", "end": "y"}], None) == []

    assert _clean(_regex_fallback("skip 1:20-2:45"), None)[0]["start_seconds"] == 80.0
    assert _clean(_regex_fallback("sponsor at 2:00"), None)[0]["end_seconds"] == 150.0
    assert _regex_fallback("no times here") == []
    print("ok")
