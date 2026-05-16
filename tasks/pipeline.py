import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from celery_app import celery
from database import SessionLocal
from models import Job, Clip, User, CreditTransaction
from services.downloader import download_video
from services.transcriber import transcribe, load_transcript
from services.diarizer import diarize
from services.analyzer import analyze_transcript, find_more_clips
from services.editor import render_and_caption_clip, probe_source_dims
from services.credits import refund
from services import r2


logger = logging.getLogger(__name__)


def _render_worker_count() -> int:
    """Concurrency cap for parallel clip rendering within a single job.

    FFmpeg itself uses multiple threads per process, so we don't want to
    oversubscribe. Default: half of CPU count, capped at 4.
    """
    env = os.getenv("CLIP_RENDER_WORKERS")
    if env and env.isdigit():
        return max(1, int(env))
    return max(1, min(4, (os.cpu_count() or 2) // 2))


# ── DB helpers (single session reused per task; thread-safe writes only on main) ──

def _set_job_status(db, job_id: str, status: str, progress: int = 0,
                    error: str = None):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return
    job.status = status
    job.stage_progress = progress
    if error:
        job.error_message = error
    if status == "ready":
        job.completed_at = datetime.utcnow()
    db.commit()


def _refund_failed_job(db, job_id: str):
    """Refund any net-outstanding deduct on this job.

    Computes (sum of deduct amounts) - (sum of refund amounts). If positive,
    refunds the difference in a single transaction. Idempotent: re-running is
    a no-op once everything is balanced.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job or not job.user_id:
        return

    deducts = db.query(CreditTransaction).filter(
        CreditTransaction.job_id == job_id,
        CreditTransaction.kind == "deduct",
    ).all()
    if not deducts:
        return

    refunds = db.query(CreditTransaction).filter(
        CreditTransaction.job_id == job_id,
        CreditTransaction.kind == "refund",
    ).all()

    deducted_total = sum(abs(d.amount) for d in deducts)
    refunded_total = sum(r.amount for r in refunds)
    outstanding = deducted_total - refunded_total
    if outstanding <= 0:
        return

    user = db.query(User).filter(User.id == job.user_id).first()
    if user:
        refund(db, user, outstanding, job_id=job_id,
               note="Auto-refund: job failed")
        db.commit()


# ── Clip rendering (parallel, with explicit error reporting) ────────────────

def _render_one_clip(job_id: str, clip_dict: dict, aspect_ratio: str,
                     transcript: dict, source_dims: tuple) -> dict:
    """Pure function — runs in a thread, no DB access.

    Returns: {clip_id, final_clip_path, error}
    """
    result = render_and_caption_clip(
        job_id,
        clip_dict,
        aspect_ratio=aspect_ratio,
        include_captions=True,
        transcript=transcript,
        source_dims=source_dims,
        profile="preview",
    )
    return {
        "clip_id": clip_dict["_clip_id"],
        "final_clip_path": result["final_clip_path"],
        "error": result["error"],
    }


def _render_clips_parallel(db, job_id: str, clips: list, aspect_ratio: str,
                            transcript: dict, source_dims: tuple,
                            progress_offset: int = 0):
    """Render N clips concurrently. Each clip row updated as its thread finishes."""
    if not clips:
        return

    workers = _render_worker_count()
    total = len(clips)
    done = 0

    # Pre-flight: mark all as rendering
    for clip_row in clips:
        clip_row.status = "rendering"
        clip_row.error_message = None
    db.commit()

    # Build per-clip dicts (capturing only what threads need — no ORM objects)
    payloads = [
        {
            "_clip_id": c.id,
            "rank": c.rank,
            "start_seconds": c.start_seconds,
            "end_seconds": c.end_seconds,
            "user_start_seconds": c.user_start_seconds,
            "user_end_seconds": c.user_end_seconds,
        }
        for c in clips
    ]
    by_id = {c.id: c for c in clips}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_render_one_clip, job_id, p, aspect_ratio,
                        transcript, source_dims)
            for p in payloads
        ]
        for fut in as_completed(futures):
            res = fut.result()
            clip_row = by_id.get(res["clip_id"])
            if not clip_row:
                continue
            if res["error"]:
                clip_row.status = "failed"
                clip_row.error_message = res["error"]
                logger.warning("clip %s render failed: %s",
                               res["clip_id"], res["error"])
            else:
                clip_row.final_clip_path = res["final_clip_path"]
                clip_row.raw_clip_path = None  # No separate raw in single-pass
                clip_row.status = "ready"
                # Push to R2 in background — serve via CDN, free egress.
                if r2.is_enabled() and res["final_clip_path"]:
                    key = r2.clip_key(job_id, clip_row.rank)
                    clip_row.r2_clip_key = key
                    _clip_id = clip_row.id
                    def _clear_key(k, cid=_clip_id):
                        _db = SessionLocal()
                        try:
                            c = _db.query(Clip).filter(Clip.id == cid).first()
                            if c and c.r2_clip_key == k:
                                c.r2_clip_key = None
                                _db.commit()
                        finally:
                            _db.close()
                    r2.upload_in_background(res["final_clip_path"], key,
                                            on_failure=_clear_key)
            db.commit()
            done += 1
            pct = progress_offset + int(done / total * (100 - progress_offset))
            _set_job_status(db, job_id, "clipping", pct)


def _insert_candidate_clips(db, job_id: str, candidates: list,
                             video_duration: float, rank_offset: int = 0) -> list:
    """Validate + insert clip candidates. Returns the new Clip rows."""
    new_rows = []
    for i, candidate in enumerate(candidates):
        start = candidate.get("start_seconds")
        end = candidate.get("end_seconds")
        if start is None or end is None or end <= start:
            logger.warning("dropping invalid clip candidate (start=%s end=%s)",
                           start, end)
            continue
        if video_duration and (start < 0 or end > video_duration + 1):
            logger.warning("dropping out-of-range clip (%.1f-%.1f, duration=%.1f)",
                           start, end, video_duration)
            continue
        duration = end - start
        if duration < 1:
            continue
        tags = json.dumps(candidate.get("tags", []))
        clip = Clip(
            job_id=job_id,
            rank=rank_offset + i + 1,
            start_seconds=start,
            end_seconds=end,
            duration_seconds=duration,
            clip_type=candidate.get("clip_type", "quotable"),
            score=candidate.get("score", 0.5),
            reason=candidate.get("reason", ""),
            transcript_excerpt=candidate.get("transcript_excerpt", ""),
            hook_line=candidate.get("hook_line"),
            tags=tags,
            status="pending",
        )
        db.add(clip)
        new_rows.append(clip)
    db.commit()
    return new_rows


# ── Main pipeline task ──────────────────────────────────────────────────────

@celery.task(bind=True, name="tasks.pipeline.run_full_pipeline")
def run_full_pipeline(self, job_id: str, options: dict):
    db = SessionLocal()
    try:
        # Stage 1: Download / accept upload
        _set_job_status(db, job_id, "downloading", 0)
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        def dl_progress(p):
            _set_job_status(db, job_id, "downloading", p)

        if job.source_type == "url":
            meta = download_video(job.source_url, job_id,
                                  progress_callback=dl_progress)
            # Mirror original to R2 in background so future re-runs / serving
            # don't need to re-download from YouTube.
            if r2.is_enabled() and meta.get("video_path"):
                key = r2.source_key(job_id)
                r2.upload_in_background(meta["video_path"], key)
                job_row = db.query(Job).filter(Job.id == job_id).first()
                if job_row and not job_row.r2_source_key:
                    job_row.r2_source_key = key
                    db.commit()
        else:
            from services.downloader import (
                _extract_audio, _get_duration, _needs_wav_extraction, get_job_dir,
            )
            job_dir = get_job_dir(job_id)
            video_path = job_dir / "original.mp4"

            # Direct-upload path: R2 already has the source. Kick AssemblyAI
            # off NOW (against the R2 URL) so its 2–5 min processing runs in
            # parallel with our R2→worker pull, audio extract, and probe.
            if job.r2_source_key and r2.is_enabled():
                try:
                    from services.transcriber import submit_async as _xcribe_submit
                    _xcribe_submit(job_id)
                except Exception as exc:
                    logger.warning("transcribe pre-submit failed for %s: %s",
                                   job_id, exc)

            # New path: direct browser→R2 upload. Pull from R2 to scratch.
            if job.r2_source_key and not video_path.exists():
                if not r2.is_enabled():
                    raise RuntimeError(
                        "Job has r2_source_key but R2 is not configured")
                _set_job_status(db, job_id, "downloading", 5)
                r2.download_file(job.r2_source_key, str(video_path))
                _set_job_status(db, job_id, "downloading", 80)

            audio_path = job_dir / "audio.wav"
            if _needs_wav_extraction():
                _extract_audio(str(video_path), str(audio_path))
            try:
                duration = _get_duration(str(video_path))
            except Exception:
                duration = None
            meta = {"title": job.source_filename, "duration": duration}

        # Probe source dimensions ONCE and cache on Job row (Phase E2)
        storage = os.getenv("STORAGE_PATH", "./storage")
        source_path = str(Path(storage) / "jobs" / job_id / "original.mp4")
        src_w = src_h = None
        try:
            src_w, src_h = probe_source_dims(source_path)
        except Exception as exc:
            logger.warning("source dim probe failed for %s: %s", job_id, exc)

        job = db.query(Job).filter(Job.id == job_id).first()
        job.video_title = meta.get("title")
        if meta.get("duration"):
            job.video_duration_seconds = float(meta["duration"])
        if src_w and src_h:
            job.source_width = src_w
            job.source_height = src_h
        db.commit()

        # Stage 2: Transcribe
        _set_job_status(db, job_id, "transcribing", 0)
        transcript = transcribe(
            job_id,
            progress_callback=lambda p: _set_job_status(db, job_id, "transcribing", p),
        )
        job = db.query(Job).filter(Job.id == job_id).first()
        job.detected_language = transcript.get("language")
        db.commit()

        # Stage 3: Diarize (non-fatal)
        _set_job_status(db, job_id, "diarizing", 0)
        try:
            diarize(
                job_id,
                progress_callback=lambda p: _set_job_status(db, job_id, "diarizing", p),
            )
        except Exception as exc:
            logger.warning("diarization failed for %s (continuing): %s",
                           job_id, exc)
            _set_job_status(db, job_id, "diarizing", 100)

        # Stage 4: Analyze
        _set_job_status(db, job_id, "analyzing", 0)
        job = db.query(Job).filter(Job.id == job_id).first()
        analysis_options = {**options, "video_title": job.video_title or ""}
        clip_candidates = analyze_transcript(
            job_id, analysis_options,
            progress_callback=lambda p: _set_job_status(db, job_id, "analyzing", p),
        )

        clips = _insert_candidate_clips(
            db, job_id, clip_candidates,
            video_duration=job.video_duration_seconds or 0,
        )

        if not clips:
            _set_job_status(db, job_id, "ready", 100)
            return

        # Stage 5: Render clips in parallel
        _set_job_status(db, job_id, "clipping", 0)
        aspect_ratio = options.get("target_aspect_ratio", "9:16")
        source_dims = (src_w, src_h) if (src_w and src_h) else None

        _render_clips_parallel(
            db, job_id, clips, aspect_ratio,
            transcript=transcript,  # passed through, no re-load per clip
            source_dims=source_dims,
        )

        _set_job_status(db, job_id, "ready", 100)

    except Exception as exc:
        logger.exception("pipeline failed for job %s", job_id)
        # Roll back any dirty state from the failed transaction before we try
        # to write the failure record + refund on a fresh transaction.
        try:
            db.rollback()
        except Exception:
            logger.exception("rollback after pipeline failure failed for %s", job_id)
        try:
            _set_job_status(db, job_id, "failed", 0, error=str(exc)[:500])
            _refund_failed_job(db, job_id)
        except Exception:
            logger.exception("status/refund write after failure failed for %s", job_id)
            try:
                db.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass


@celery.task(bind=True, name="tasks.pipeline.run_more_clips")
def run_more_clips(self, job_id: str, options: dict, excluded_clips: list):
    db = SessionLocal()
    try:
        _set_job_status(db, job_id, "analyzing", 0)
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        analysis_options = {**options, "video_title": job.video_title or ""}
        new_candidates = find_more_clips(
            job_id, excluded_clips, analysis_options,
            progress_callback=lambda p: _set_job_status(db, job_id, "analyzing", p),
        )
        if not new_candidates:
            _set_job_status(db, job_id, "ready", 100)
            return

        existing_count = db.query(Clip).filter(Clip.job_id == job_id).count()
        clips = _insert_candidate_clips(
            db, job_id, new_candidates,
            video_duration=job.video_duration_seconds or 0,
            rank_offset=existing_count,
        )
        if not clips:
            _set_job_status(db, job_id, "ready", 100)
            return

        _set_job_status(db, job_id, "clipping", 0)
        aspect_ratio = options.get("target_aspect_ratio", "9:16")
        transcript = load_transcript(job_id)
        source_dims = (job.source_width, job.source_height) \
            if (job.source_width and job.source_height) else None

        _render_clips_parallel(
            db, job_id, clips, aspect_ratio,
            transcript=transcript, source_dims=source_dims,
        )

        _set_job_status(db, job_id, "ready", 100)

    except Exception as exc:
        logger.exception("more-clips failed for job %s", job_id)
        try:
            db.rollback()
        except Exception:
            logger.exception("rollback after more-clips failure failed for %s", job_id)
        try:
            _set_job_status(db, job_id, "failed", 0, error=str(exc)[:500])
            _refund_failed_job(db, job_id)
        except Exception:
            logger.exception("status/refund write after more-clips failure failed for %s", job_id)
            try:
                db.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass
