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
from services.video_cache import required_quality, get_cache_hit, touch_cache, store_cache


logger = logging.getLogger(__name__)


@celery.task(bind=True, name="tasks.test_download")
def test_download_task(self, url: str) -> dict:
    import time, shutil, tempfile, socket, uuid
    from services.downloader import _download_via_webshare, _write_cookies_tempfile
    tmp = Path(tempfile.mkdtemp())
    print(f"[test_download] worker_ip={socket.gethostbyname(socket.gethostname())}", flush=True)
    try:
        t0 = time.time()
        import os as _os
        svc_url = _os.getenv("DOWNLOAD_SERVICE_URL", "").strip()
        if svc_url:
            from services.downloader import _download_via_mac_service
            meta = _download_via_mac_service(url, "test-" + str(uuid.uuid4())[:8], tmp, svc_url)
        else:
            from services.downloader import _download_via_webshare
            meta = _download_via_webshare(url, tmp)
        elapsed = round(time.time() - t0, 1)
        files = list(tmp.iterdir())
        size_mb = round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1) if files else 0
        return {
            "ok": True,
            "title": meta.get("title"),
            "duration_s": meta.get("duration"),
            "size_mb": size_mb,
            "elapsed_s": elapsed,
            "avg_speed_mbs": round(size_mb / elapsed, 2) if elapsed > 0 else 0,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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

    # (a) Guarantee source video exists locally. Pipeline may resume on a
    #     fresh worker (Celery retry, redeploy) where the ephemeral disk
    #     was wiped — pull it back from R2 once before spawning threads.
    try:
        from services.exporter import _ensure_local_source
        _ensure_local_source(job_id)
    except Exception as exc:
        logger.error("source restore failed for %s: %s", job_id, exc)
        # Mark every clip failed with a clear reason and stop early.
        for c in clips:
            c.status = "failed"
            c.error_message = f"source restore failed: {exc}"
        db.commit()
        return

    # (b) Idempotency: on Celery retry, some clips may already be rendered
    #     and in R2. Skip those — re-rendering costs time + risks corrupting
    #     a valid file with a half-written re-render.
    pending: list = []
    for c in clips:
        if c.status == "ready" and c.r2_clip_key and r2.is_enabled():
            try:
                if r2.object_exists(c.r2_clip_key):
                    continue  # already done, skip
            except Exception:
                pass  # any probe failure → re-render to be safe
        pending.append(c)

    if not pending:
        return

    workers = _render_worker_count()
    total = len(pending)
    done = 0

    # Pre-flight: mark pending as rendering
    for clip_row in pending:
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
        for c in pending
    ]
    by_id = {c.id: c for c in pending}

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
                # (c) Upload to R2 SYNCHRONOUSLY before marking ready.
                #     Daemon-thread uploads die on Railway redeploy → clip row
                #     ends up pointing at a non-existent R2 object → user
                #     downloads return 404. Block until R2 confirms, then commit.
                if r2.is_enabled() and res["final_clip_path"]:
                    key = r2.clip_key(job_id, clip_row.rank)
                    try:
                        r2.upload_file(res["final_clip_path"], key)
                        if r2.object_exists(key):
                            clip_row.r2_clip_key = key
                            clip_row.status = "ready"
                        else:
                            clip_row.status = "failed"
                            clip_row.error_message = "R2 upload verify failed"
                    except Exception as upload_exc:
                        logger.exception("R2 upload failed for clip %s",
                                         clip_row.id)
                        clip_row.status = "failed"
                        clip_row.error_message = f"R2 upload: {upload_exc}"[:500]
                else:
                    clip_row.status = "ready"
            db.commit()
            done += 1
            pct = progress_offset + int(done / total * (100 - progress_offset))
            _set_job_status(db, job_id, "clipping", pct)


def _insert_candidate_clips(db, job_id: str, candidates: list,
                             video_duration: float, rank_offset: int = 0) -> list:
    """Validate + insert clip candidates. Returns the new Clip rows."""
    from models import Payment, CreditTransaction
    job = db.query(Job).filter(Job.id == job_id).first()
    credit_type = "free"
    if job and job.user_id:
        user = db.query(User).filter(User.id == job.user_id).first()
        if user:
            has_payment = db.query(Payment).filter(Payment.user_id == user.id, Payment.status == "success").first() is not None
            has_admin_grant = db.query(CreditTransaction).filter(CreditTransaction.user_id == user.id, CreditTransaction.kind == "admin_grant").first() is not None
            if user.subscription_tier_id or has_payment or has_admin_grant:
                credit_type = "paid"

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
            credit_type=credit_type,
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
            # Determine quality from subscription tier
            user = db.query(User).filter(User.id == job.user_id).first()
            quality = required_quality(user) if user else "720p"

            # Check R2 cache first — avoid re-downloading the same video
            cache_entry = get_cache_hit(db, job.source_url, quality)
            if cache_entry:
                cached_key = cache_entry.r2_key_1080p if quality == "1080p" else cache_entry.r2_key_720p
                print(f"[pipeline] cache hit video_id={cache_entry.video_id} quality={quality} key={cached_key}", flush=True)
                job_dir = Path(os.getenv("STORAGE_PATH", "./storage")) / "jobs" / job_id
                job_dir.mkdir(parents=True, exist_ok=True)
                local_path = job_dir / "original.mp4"
                r2.download_file(cached_key, str(local_path))
                touch_cache(db, cache_entry)
                # Point job r2_source_key at the cached object so re-runs don't re-download
                job_row = db.query(Job).filter(Job.id == job_id).first()
                if job_row and not job_row.r2_source_key:
                    job_row.r2_source_key = cached_key
                    db.commit()
                meta = {
                    "title": cache_entry.title,
                    "duration": cache_entry.duration,
                    "video_path": str(local_path),
                    "audio_path": str(job_dir / "audio.wav"),
                }
                dl_progress(100)
            else:
                # Fresh download — tell mac service which quality to fetch and where to put it
                import re as _re
                _vid_match = _re.search(r"[?&]v=([a-zA-Z0-9_-]{11})|youtu\.be/([a-zA-Z0-9_-]{11})|/shorts/([a-zA-Z0-9_-]{11})", job.source_url or "")
                _vid_id = next((g for g in (_vid_match.groups() if _vid_match else []) if g), "")
                cache_r2_key = f"cache/{_vid_id}/{quality}.mp4" if _vid_id else ""

                meta = download_video(job.source_url, job_id,
                                      progress_callback=dl_progress,
                                      quality=quality,
                                      cache_r2_key=cache_r2_key)

                # Persist in cache table so next user benefits
                if _vid_id and cache_r2_key and meta.get("video_path"):
                    store_cache(
                        db, job.source_url, quality, cache_r2_key,
                        title=meta.get("title", ""),
                        duration=float(meta.get("duration") or 0),
                    )

                # Mirror original to R2 in background (for clip re-runs on fresh workers)
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

            # NOTE: we used to pre-submit to AssemblyAI here against the R2
            # URL, but that sent the FULL 1-2GB video. AAI had to download it
            # all before extracting audio → 30+ min for a 2hr podcast.
            # Now we always extract compact audio locally (~30s) and upload
            # that to AAI in stage 2. Total transcription drops to ~5 min.

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
            duration = job.video_duration_seconds or 0
            if duration > 1800:  # > 30 min — zero clips is almost certainly a bug
                logger.error(
                    "Zero clips from a %.0f-minute video (job %s). "
                    "This is likely an LLM analysis failure, not a content quality issue. "
                    "Check analyzer logs for OpenRouter errors.",
                    duration / 60, job_id,
                )
                _set_job_status(
                    db, job_id, "ready", 100,
                    error="AI analysis completed but found no clip-worthy moments. "
                          "This is unusual for long content — try regenerating.",
                )
            else:
                logger.warning("No clip candidates for job %s (duration=%.0fs)", job_id, duration)
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
