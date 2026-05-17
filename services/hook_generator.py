"""Generate short viral hook text suggestions from a clip's transcript.

Uses a small/cheap OpenRouter model (configurable via HOOK_GEN_MODEL,
default google/gemini-2.5-flash-lite). Returns up to N strings.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import List

from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("HOOK_GEN_MODEL", "google/gemini-2.5-flash-lite")
MAX_HOOKS = 4
MAX_HOOK_CHARS = 90


def _client() -> OpenAI:
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )


def _build_prompt(excerpt: str, existing_hook: str | None) -> str:
    excerpt = (excerpt or "").strip()[:1500]
    parts = [
        "You are a short-form video copywriter. Write viral hook overlays "
        "for a vertical (TikTok/Reels/Shorts) clip.",
        "",
        "Constraints:",
        f"- Each hook MUST be <= {MAX_HOOK_CHARS} characters.",
        "- Plain text only. No quotes, no emojis, no hashtags, no markdown.",
        "- Punchy, curiosity-driven, scroll-stopping. Question, bold claim, "
        "or POV format encouraged.",
        "- Match the clip topic/voice from the transcript below.",
        f"- Return EXACTLY {MAX_HOOKS} distinct hook options, each on its own line.",
        "- No numbering, no bullets, no commentary — only the hook lines.",
        "",
        "Transcript:",
        excerpt or "(no transcript available — invent a generic curiosity hook)",
    ]
    if existing_hook:
        parts.extend(["", f"Avoid repeating this existing hook: {existing_hook}"])
    return "\n".join(parts)


def _parse_lines(raw: str) -> List[str]:
    lines = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        # strip leading bullets / numbering ("1.", "1)", "-", "*")
        s = re.sub(r"^[\-\*•\d]+[\.\)]?\s*", "", s)
        # strip surrounding quotes
        s = s.strip().strip("\"'“”‘’")
        if not s:
            continue
        if len(s) > MAX_HOOK_CHARS:
            s = s[:MAX_HOOK_CHARS].rstrip()
        lines.append(s)
    # dedupe preserving order
    seen = set()
    out = []
    for s in lines:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out[:MAX_HOOKS]


def generate_hooks(excerpt: str, existing_hook: str | None = None) -> List[str]:
    """Return up to MAX_HOOKS viral hook strings."""
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    client = _client()
    prompt = _build_prompt(excerpt, existing_hook)
    try:
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL,
            max_tokens=400,
            temperature=0.9,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.exception("hook generation LLM call failed")
        raise RuntimeError(f"LLM call failed: {exc}") from exc

    raw = (resp.choices[0].message.content or "").strip()
    # some models wrap in code fences or JSON — handle both gracefully
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json|text)?\n?|\n?```$", "", raw).strip()
    # if model returned JSON array, parse it
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return _parse_lines("\n".join(str(x) for x in arr))
        except Exception:
            pass
    return _parse_lines(raw)
