import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from pathlib import Path
from services.transcriber import load_transcript
from services.diarizer import load_diarization, build_enriched_transcript

logger = logging.getLogger(__name__)


CHUNK_WINDOW_SECONDS = 1200  # 20-minute windows keep each prompt under ~30k tokens


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

    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(clips, f, ensure_ascii=False, indent=2)

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
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )
    chunks = _chunk_enriched(enriched, CHUNK_WINDOW_SECONDS)
    max_clips = options.get("max_clips", 5)

    if progress_callback:
        progress_callback(10)

    all_candidates = _analyze_chunks_parallel(client, transcript, chunks, options,
                                              progress_callback, base_progress=10,
                                              span=70)
    all_candidates.sort(key=lambda c: c.get("score", 0), reverse=True)
    clips = _deduplicate(all_candidates, max_clips, pre_selected=pre_selected or [])

    if not clips:
        options_relaxed = {**options, "_relaxed": True}
        all_relaxed = _analyze_chunks_parallel(
            client, transcript, chunks, options_relaxed,
            progress_callback, base_progress=80, span=15,
        )
        all_relaxed.sort(key=lambda c: c.get("score", 0), reverse=True)
        clips = _deduplicate(all_relaxed, max_clips, pre_selected=pre_selected or [])
        for c in clips:
            c["_below_threshold"] = True

    for i, c in enumerate(clips):
        c["rank"] = i + 1

    return clips


def _llm_call(client: OpenAI, transcript: dict, chunk: list, options: dict) -> list:
    """Single chunk → list of candidate dicts. Returns [] on failure."""
    prompt = _build_prompt(transcript, chunk, options)
    try:
        response = client.chat.completions.create(
            model="anthropic/claude-sonnet-4-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_clips(response.choices[0].message.content.strip())
    except Exception as exc:
        logger.warning("OpenRouter chunk call failed: %s", exc)
        return []


def _analyze_chunks_parallel(client: OpenAI, transcript: dict, chunks: list,
                              options: dict, progress_callback, base_progress: int,
                              span: int) -> list:
    """Dispatch all chunks in parallel via thread pool. Aggregates results."""
    if not chunks:
        return []
    workers = min(len(chunks), int(os.getenv("ANALYZER_CHUNK_WORKERS", "3")))
    candidates: list = []
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_llm_call, client, transcript, chunk, options)
                   for chunk in chunks]
        from concurrent.futures import as_completed as _as_completed
        for fut in _as_completed(futures):
            candidates.extend(fut.result())
            completed += 1
            if progress_callback:
                progress_callback(base_progress + int(completed / len(chunks) * span))
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

    return f"""You are a world-class short-form content strategist who has studied thousands of viral Indian Reels, YouTube Shorts, and podcast clips. You understand the psychology of why Indian audiences stop scrolling, watch till the end, and hit share.

Your task: analyze this transcript and extract the {max_clips} segments most likely to go viral as standalone short-form clips. Each clip must be a SINGLE UNBROKEN continuous segment — never stitched, never rearranged.

Think like a viewer, not an editor. Ask yourself: "If I was scrolling at 2am and this clip autoplayed, would I stop? Would I watch till the end? Would I send it to someone?"

---

VIDEO METADATA:
- Title: {title}
- Total Duration: {duration_fmt}
- Language: {language}
- Clip Types to Find: {", ".join(clip_types)}
- Duration Range: {min_dur}s – {max_dur}s per clip

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

Return up to {max_clips} clips ranked by score. Only include clips scoring above {threshold}. Be selective — 3 great clips beat 5 mediocre ones.
"""


def _parse_clips(text: str) -> list:
    # Strip markdown code fences if present
    if "```" in text:
        lines = text.split("\n")
        inner = []
        in_block = False
        for line in lines:
            if line.strip().startswith("```"):
                in_block = not in_block
                continue
            if in_block or not line.strip().startswith("```"):
                inner.append(line)
        text = "\n".join(inner)

    try:
        clips = json.loads(text)
        if isinstance(clips, list):
            for i, c in enumerate(clips):
                if "rank" not in c:
                    c["rank"] = i + 1
            return clips
    except json.JSONDecodeError:
        pass
    return []
