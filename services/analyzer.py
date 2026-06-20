import os
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from services.transcriber import load_transcript
from services.diarizer import load_diarization, build_enriched_transcript
from services.llm_provider import generate_text, model_for, provider_name

logger = logging.getLogger(__name__)


CHUNK_WINDOW_SECONDS = 1200  # 20-minute windows keep each prompt under ~30k tokens
_LLM_MAX_RETRIES = 3
_LLM_BASE_DELAY = 2  # seconds; doubles each retry


def analyze_transcript(job_id: str, options: dict, progress_callback=None) -> list:
    storage = os.getenv("STORAGE_PATH", "./storage")
    analysis_path = Path(storage) / "jobs" / job_id / "analysis.json"

    if analysis_path.exists():
        if progress_callback:
            progress_callback(100)
        with open(analysis_path, "r", encoding="utf-8") as f:
            return json.load(f)

    transcript = load_transcript(job_id)
    diarization = load_diarization(job_id)
    enriched = build_enriched_transcript(transcript, diarization)

    clips = _run_analysis(job_id, transcript, enriched, options, progress_callback)

    # Only cache non-empty results. If the LLM failed on all chunks, we don't
    # want to persist an empty array — that would make retries load the cached
    # empty list instead of re-running the analysis.
    if clips:
        with open(analysis_path, "w", encoding="utf-8") as f:
            json.dump(clips, f, ensure_ascii=False, indent=2)
    else:
        logger.warning("Skipping analysis cache — zero clips produced for job %s", job_id)

    if progress_callback:
        progress_callback(100)

    return clips


def find_more_clips(job_id: str, excluded_clips: list, options: dict, progress_callback=None) -> list:
    """Run a fresh analysis pass excluding already-selected clip ranges."""
    transcript = load_transcript(job_id)
    diarization = load_diarization(job_id)
    enriched = build_enriched_transcript(transcript, diarization)

    new_clips = _run_analysis(
        job_id, transcript, enriched, options,
        progress_callback, pre_selected=excluded_clips
    )

    return new_clips


def _run_analysis(_job_id: str, transcript: dict, enriched: list, options: dict,
                  progress_callback=None, pre_selected: list = None) -> list:
    chunks = _chunk_enriched(enriched, CHUNK_WINDOW_SECONDS)
    max_clips = options.get("max_clips", 5)

    if progress_callback:
        progress_callback(10)

    all_candidates = _analyze_chunks_parallel(transcript, chunks, options,
                                              progress_callback, base_progress=10,
                                              span=70)
    all_candidates.sort(key=lambda c: c.get("score", 0), reverse=True)
    clips = _deduplicate(all_candidates, max_clips, pre_selected=pre_selected or [])

    if not clips:
        options_relaxed = {**options, "_relaxed": True}
        all_relaxed = _analyze_chunks_parallel(
            transcript, chunks, options_relaxed,
            progress_callback, base_progress=80, span=15,
        )
        all_relaxed.sort(key=lambda c: c.get("score", 0), reverse=True)
        clips = _deduplicate(all_relaxed, max_clips, pre_selected=pre_selected or [])
        for c in clips:
            c["_below_threshold"] = True

    for i, c in enumerate(clips):
        c["rank"] = i + 1

    return clips


def _llm_call(transcript: dict, chunk: list, options: dict) -> list:
    """Single chunk → list of candidate dicts. Returns [] on failure.

    Retries up to _LLM_MAX_RETRIES times with exponential backoff to handle
    transient OpenRouter 429/500 errors that silently kill long-podcast analysis.
    """
    prompt = _build_prompt(transcript, chunk, options)
    model = model_for(
        "json",
        openrouter_default="anthropic/claude-sonnet-4-5",
        gemini_default="gemini-3.5-flash",
    )
    provider = provider_name()
    max_tokens = int(os.getenv("ANALYZER_MAX_TOKENS", "4096"))
    chunk_start = chunk[0]["start"] if chunk else 0
    chunk_end = chunk[-1]["end"] if chunk else 0
    prompt_len = len(prompt)

    logger.info(
        "LLM call: chunk %.0f-%.0fs, prompt_len=%d chars, provider=%s, model=%s, max_tokens=%d",
        chunk_start, chunk_end, prompt_len, provider, model, max_tokens,
    )

    last_exc = None
    for attempt in range(1, _LLM_MAX_RETRIES + 1):
        try:
            raw = generate_text(
                prompt,
                purpose="json",
                openrouter_model=model,
                gemini_model=model,
                max_tokens=max_tokens,
                json_response=True,
            )
            raw = raw.strip()
            logger.info(
                "LLM response: chunk %.0f-%.0fs, response_len=%d chars (attempt %d)",
                chunk_start, chunk_end, len(raw), attempt,
            )
            clips = _parse_clips(raw)
            if clips:
                logger.info(
                    "Parsed %d clip candidates from chunk %.0f-%.0fs",
                    len(clips), chunk_start, chunk_end,
                )
                return clips
            else:
                # Model returned a response but no parseable clips —
                # log the raw output for debugging and retry.
                logger.warning(
                    "LLM returned unparseable/empty response for chunk %.0f-%.0fs "
                    "(attempt %d/%d). Raw response (first 500 chars): %s",
                    chunk_start, chunk_end, attempt, _LLM_MAX_RETRIES,
                    raw[:500],
                )
                last_exc = ValueError("Empty/unparseable LLM response")
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "%s chunk call failed for %.0f-%.0fs (attempt %d/%d): %s",
                provider, chunk_start, chunk_end, attempt, _LLM_MAX_RETRIES, exc,
            )

        if attempt < _LLM_MAX_RETRIES:
            delay = _LLM_BASE_DELAY * (2 ** (attempt - 1))
            logger.info("Retrying in %ds...", delay)
            time.sleep(delay)

    logger.error(
        "All %d LLM attempts failed for chunk %.0f-%.0fs. Last error: %s",
        _LLM_MAX_RETRIES, chunk_start, chunk_end, last_exc,
    )
    return []


def _analyze_chunks_parallel(transcript: dict, chunks: list,
                              options: dict, progress_callback, base_progress: int,
                              span: int) -> list:
    """Dispatch all chunks in parallel via thread pool. Aggregates results.

    Caps parallelism at 3 for large jobs to avoid OpenRouter rate limits
    that were silently killing all chunk calls for 2hr podcasts.
    """
    if not chunks:
        return []
    # Cap at 3 concurrent LLM calls — OpenRouter rate-limits when we blast 6+
    # requests simultaneously for long podcasts. Each request is large (~30k
    # tokens), so the per-minute token budget gets exhausted.
    default_workers = "3" if len(chunks) > 4 else "4"
    workers = min(len(chunks), int(os.getenv("ANALYZER_CHUNK_WORKERS", default_workers)))
    candidates: list = []
    completed = 0
    failed_chunks = 0

    logger.info("Analyzing %d chunks with %d parallel workers", len(chunks), workers)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_llm_call, transcript, chunk, options)
                   for chunk in chunks]
        from concurrent.futures import as_completed as _as_completed
        for fut in _as_completed(futures):
            result = fut.result()
            if result:
                candidates.extend(result)
            else:
                failed_chunks += 1
            completed += 1
            if progress_callback:
                progress_callback(base_progress + int(completed / len(chunks) * span))

    if failed_chunks > 0:
        logger.warning(
            "%d/%d chunks returned zero candidates. Total candidates so far: %d",
            failed_chunks, len(chunks), len(candidates),
        )
    if not candidates:
        logger.error(
            "ALL %d chunks returned zero candidates — LLM analysis produced nothing. "
            "This usually means the model is timing out, rate-limited, or returning "
            "malformed JSON. Check the configured LLM provider dashboard for errors.",
            len(chunks),
        )

    return candidates


def _chunk_enriched(enriched: list, window_seconds: int = 1200) -> list:
    chunks: list = []
    current: list = []
    for seg in enriched:
        if current and seg["start"] - current[0]["start"] > window_seconds:
            chunks.append(current)
            current = []
        current.append(seg)
    if current:
        chunks.append(current)
    return chunks


def _deduplicate(candidates: list, max_clips: int, pre_selected: list = None) -> list:
    """Remove candidates that overlap >30% with any already-selected clip (measured against either clip)."""
    pre = list(pre_selected or [])
    selected = []
    for c in candidates:
        start, end = c.get("start_seconds", 0), c.get("end_seconds", 0)
        duration = end - start
        if duration <= 0:
            continue
        overlap = False
        for s in pre + selected:
            s_start, s_end = s.get("start_seconds", 0), s.get("end_seconds", 0)
            s_duration = s_end - s_start
            overlap_secs = max(0, min(end, s_end) - max(start, s_start))
            if s_duration > 0 and (overlap_secs / duration > 0.3 or overlap_secs / s_duration > 0.3):
                overlap = True
                break
        if not overlap:
            selected.append(c)
        if len(selected) >= max_clips:
            break
    return selected


def _build_prompt(transcript: dict, enriched: list, options: dict) -> str:
    language = transcript.get("language", "unknown")
    duration_s = sum(seg["end"] - seg["start"] for seg in enriched)
    mins = int(duration_s // 60)
    secs = int(duration_s % 60)
    duration_fmt = f"{mins}m {secs}s"

    clip_types = options.get("clip_types", ["controversy", "hook_intro", "quotable", "shocking_stat", "myth_bust"])
    min_dur = options.get("min_clip_duration", 20)
    max_dur = options.get("max_clip_duration", 90)
    max_clips = options.get("max_clips", 5)
    title = options.get("video_title", "Unknown")
    threshold = 0.5 if options.get("_relaxed") else 0.65
    custom_prompt = (options.get("custom_prompt") or "").strip()

    enriched_text = "\n".join(
        f"[{seg['start']:.1f}s | {seg['speaker']}] {seg['text']}"
        for seg in enriched
    )

    clip_type_defs = """
1. **controversy** — A claim or opinion that divides the audience. Triggers debate, shares, comments.
2. **hook_intro** — Best 15-30s attention-grabbing opener. Must make someone stop scrolling instantly.
3. **quotable** — Short, punchy, standalone insight. Could be a tweet. Works without any context.
4. **myth_bust** — Debunks something the audience believed. Signals: "Actually...", "Most people think X but...", "Yeh galat hai", "Asal sach yeh hai", "Log nahi jaante ki...", "Jo aap sochte hain wo sach nahi", "Bahut log mante hain ki X, lekin...". Must include BOTH the wrong belief AND the correction — not just one side.
5. **shocking_stat** — Specific surprising number or data point that reframes something familiar.
6. **emotional_peak** — Genuine raw emotion: vulnerability, grief, joy, anger, passion, breakthrough moment.
7. **debate_moment** — Two speakers strongly disagree. Real tension, pushback, interrupted sentences.
8. **story_arc** — Complete mini-story: problem → struggle → resolution. Satisfying standalone.
"""

    viral_patterns = """
PROVEN VIRAL OPENING PATTERNS (first 3 seconds make or break retention):
- "X साल में पहली बार..." / "For the first time in X years..."
- A bold claim stated with total confidence
- A number that shocks: "₹50 crore", "10,000 children", "90% of Indians"
- Direct challenge to common belief: "Jo aap sochte hain wo galat hai", "Yeh sach nahi hai", "Log galat sochte hain"
- Myth reveal opener: clip starts with the wrong belief, then immediately flips it — viewer stays to see the truth
- Mid-story drop: clip starts mid-revelation, viewer must watch to understand
- Two people arguing — tension is immediately visible
- Speaker's voice breaks or shows strong emotion in first 5 seconds
- Price/market revelation: "Kisan ko sirf ₹X milta hai lekin aap ₹Y dete ho" — systemic injustice hooks instantly

ANTI-PATTERNS — avoid these (they kill retention):
- Clip starts with greetings, intros, or "aaj hum baat karenge..."
- Long setup with no payoff within first 10 seconds
- Clip ends mid-sentence or mid-thought
- Content only makes sense if you've watched the full episode
- Speaker trailing off, long pauses, filler-heavy sections
- Pure information with no opinion, story, or emotion
"""

    cultural_context = """
INDIAN CONTENT CULTURAL DEPTH:
- **Health myths**: Claims contradicting "ghee is bad", "rice makes you fat", "protein only from meat" → extremely viral
- **Food & agriculture myths**: "Imported is better than desi", "expensive fruit = healthy", price gaps between farm and retail (kisan ko ₹5 mila, aap ne ₹80 diya), middleman exploitation, seasonal food myths — huge shareability among middle-class Indians
- **Market & supply chain expose**: Revealing how much farmer gets vs. consumer pays, why certain foods are expensive, who profits in the chain → outrage + share trigger
- **Education system**: IIT/IIM, coaching culture, marks pressure, NEET/UPSC → massive relatability for 18-35 age group
- **Money & class**: First-generation wealth, salary reveals, "mere baap ne itna kamaya", real estate prices → high shares
- **Family dynamics**: Parents' sacrifices, arranged marriage, joint family conflict, in-laws → deep emotional resonance
- **Hinglish code-switching**: Natural, do not penalize. "Basically", "actually", "I mean" in Hindi speech = authenticity marker
- **Doctors/experts contradicting popular advice**: "Jo doctor bolta hai wo sach nahi" → controversy + shareability
- **Politicians/system critique**: Careful framing, high controversy score if specific and evidence-backed
- **Cricket/Bollywood anecdotes**: Personal stories involving these → instant cultural hook
- **Startup/hustle culture**: Failure stories, funding amounts, "bootstrapped to ₹X crore" → aspirational shares
- **Religious/spiritual claims**: High controversy potential, score carefully based on framing
- **Desi vs. foreign**: Any content that challenges "videshi cheez better hai" or reveals India produces something the world uses → pride + share
"""

    # Build dynamic sections that change when a custom prompt is provided
    if custom_prompt:
        task_line = (
            f"Your task: analyze this transcript and extract the {max_clips} segments that best fulfill "
            f"the USER INTENT below. Each clip must be a SINGLE UNBROKEN continuous segment — never stitched, never rearranged."
        )
        intent_block = f"\n\nUSER INTENT (highest priority — this overrides generic virality scoring):\n{custom_prompt}\n\nEverything else — virality patterns, cultural context, clip-type definitions — serves as supporting guidance. Always ask: does this clip best serve the stated intent?"
        clip_types_line = f"- Goal: {custom_prompt[:120]}{'...' if len(custom_prompt) > 120 else ''}"
    else:
        task_line = (
            f"Your task: analyze this transcript and extract the {max_clips} segments most likely to go viral "
            f"as standalone short-form clips. Each clip must be a SINGLE UNBROKEN continuous segment — never stitched, never rearranged."
        )
        intent_block = ""
        clip_types_line = f"- Clip Types to Find: {', '.join(clip_types)}"

    return f"""You are a world-class short-form content strategist who has studied thousands of viral Indian Reels, YouTube Shorts, and podcast clips. You understand the psychology of why Indian audiences stop scrolling, watch till the end, and hit share.

{task_line}

Think like a viewer, not an editor. Ask yourself: "If I was scrolling at 2am and this clip autoplayed, would I stop? Would I watch till the end? Would I send it to someone?"{intent_block}

---

VIDEO METADATA:
- Title: {title}
- Total Duration: {duration_fmt}
- Language: {language}
{clip_types_line}
- Duration Range: {min_dur}s – {max_dur}s per clip
- Hard limit: end_seconds - start_seconds MUST be <= {max_dur}. Never return a clip longer than {max_dur} seconds.
- Any clip longer than {max_dur} seconds is INVALID and will be rejected by the system.

---

TRANSCRIPT (format: [seconds | SPEAKER] text):
{enriched_text}

---

CLIP TYPES:
{clip_type_defs}

---

{viral_patterns}

---

{cultural_context}

---

VIRALITY SCORE (0.0 – 1.0) — weight each dimension:
1. **Hook strength (0.30)**: Does the clip's opening line make someone stop scrolling? Is it a bold claim, surprising fact, or raw emotion?
2. **Retention pull (0.25)**: Once watching, is there a reason to stay till the end? Unresolved tension, building revelation, emotional journey?
3. **Shareability trigger (0.20)**: Would someone forward this? ("Yaar ye dekh", "this is so true", "OMG I didn't know this")
4. **Standalone completeness (0.15)**: Does it make full sense without watching the full video?
5. **Emotional charge (0.10)**: Does it make you feel something — anger, surprise, inspiration, nostalgia, laughter?

Score 0.9+ only for moments that are genuinely exceptional. Be honest and critical.

---

CLIP BOUNDARY RULES:
- NON-NEGOTIABLE: end_seconds - start_seconds must be >= {min_dur} and <= {max_dur}. Recalculate every candidate before returning JSON.
- If the full idea is longer than {max_dur}s, return only the strongest continuous {max_dur}s-or-less section.
- Do not solve duration by stitching multiple sections. Use one continuous timestamp range only.
- Start 1–2s BEFORE the key moment (viewer needs just enough context)
- End 1–2s AFTER the thought is complete (let it land)
- Never cut mid-sentence unless the speaker is being interrupted (that's fine — tension)
- For myth_bust and shocking_stat: ALWAYS include both the setup AND the reveal
- For story_arc: must have a clear beginning AND resolution within the clip window
- Ideal clip: starts with something that makes you lean in, ends with something that makes you think

---

OUTPUT: Respond ONLY with a valid JSON array. No explanation, no markdown, no preamble.

[
  {{
    "rank": 1,
    "start_seconds": 142.5,
    "end_seconds": 187.2,
    "clip_type": "shocking_stat",
    "score": 0.91,
    "hook_line": "The exact opening words that will hook the viewer (first sentence of clip)",
    "reason": "Specific explanation of WHY this moment is viral — reference the hook, the tension, the emotional trigger, and the cultural resonance",
    "transcript_excerpt": "Exact words spoken in this segment (first 2-3 sentences)",
    "retention_reason": "Why will someone watch this till the end?",
    "share_trigger": "What specific thing will make someone share this?",
    "tags": ["health", "indian-diet", "shocking"]
  }}
]

Before finalizing, verify every object satisfies:
- end_seconds > start_seconds
- end_seconds - start_seconds >= {min_dur}
- end_seconds - start_seconds <= {max_dur}

Return up to {max_clips} clips ranked by score. Only include clips scoring above {threshold}. Be selective — 3 great clips beat 5 mediocre ones. Do not include any object that violates the duration rules.
"""


def _parse_clips(text: str) -> list:
    """Extract a JSON array of clip candidates from LLM output.

    Handles: raw JSON, markdown-fenced JSON, and truncated JSON from
    max_tokens cutoff (the #1 cause of 'no clips' on long podcasts).
    """
    if not text:
        return []

    # Strategy 1: Strip markdown code fences and extract inner content
    if "```" in text:
        # Use regex to extract content between first ``` and last ```
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    # Strategy 2: Try parsing as-is
    clips = _try_parse_json_array(text)
    if clips is not None:
        return clips

    # Strategy 3: Extract the first JSON array from the text (model may have
    # added commentary before/after)
    match = re.search(r"(\[\s*\{.*)", text, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
        clips = _try_parse_json_array(candidate)
        if clips is not None:
            return clips

        # Strategy 4: Fix truncated JSON from max_tokens cutoff.
        # The model ran out of output tokens mid-JSON. Try to salvage
        # whatever complete objects exist before the truncation point.
        clips = _salvage_truncated_json(candidate)
        if clips:
            logger.warning(
                "Salvaged %d clips from truncated JSON (model hit max_tokens)",
                len(clips),
            )
            return clips

    logger.warning("Failed to parse any clips from LLM output (len=%d)", len(text))
    return []


def _try_parse_json_array(text: str) -> list | None:
    """Try to parse text as a JSON array. Returns list or None."""
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for i, c in enumerate(data):
                if "rank" not in c:
                    c["rank"] = i + 1
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _salvage_truncated_json(text: str) -> list:
    """Extract complete JSON objects from a truncated array.

    When the LLM hits max_tokens mid-output, the JSON array is cut off like:
      [{...}, {...}, {"start  ← truncated here
    We extract all complete objects before the cut.
    """
    results = []
    # Find all complete top-level objects within the array
    depth = 0
    obj_start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    obj = json.loads(text[obj_start:i + 1])
                    if isinstance(obj, dict) and "start_seconds" in obj:
                        if "rank" not in obj:
                            obj["rank"] = len(results) + 1
                        results.append(obj)
                except (json.JSONDecodeError, ValueError):
                    pass
                obj_start = None
    return results
