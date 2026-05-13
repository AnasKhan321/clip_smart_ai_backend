import json
from datetime import datetime
from celery_app import celery
from database import SessionLocal
from models import Job, Clip, User
from services.downloader import download_video
from services.transcriber import transcribe
from services.diarizer import diarize
from services.analyzer import analyze_transcript, find_more_clips
from services.editor import render_clip, burn_captions
from services.transcriber import load_transcript
from services.credits import refund


def _refund_failed_job(job_id: str):
    """Refund credits to job owner when job fails."""
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job or not job.user_id:
            return
        # Find original deduction (negative amount tied to this job)
        from models import CreditTransaction
        original = (
            db.query(CreditTransaction)
            .filter(
                CreditTransaction.job_id == job_id,
                CreditTransaction.kind == "deduct",
            )
            .first()
        )
        if not original:
            return
        already_refunded = (
            db.query(CreditTransaction)
            .filter(
                CreditTransaction.job_id == job_id,
                CreditTransaction.kind == "refund",
            )
            .first()
        )
        if already_refunded:
            return

        user = db.query(User).filter(User.id == job.user_id).first()
        if user:
            refund(db, user, abs(original.amount), job_id=job_id, note="Auto-refund: job failed")
            db.commit()
    finally:
        db.close()


def _update_job(job_id: str, status: str, progress: int, error: str = None):
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            job.status = status
            job.stage_progress = progress
            if error:
                job.error_message = error
            if status == "ready":
                job.completed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    if status == "failed":
        _refund_failed_job(job_id)


@celery.task(bind=True, name="tasks.pipeline.run_full_pipeline")
def run_full_pipeline(self, job_id: str, options: dict):
    db = SessionLocal()
    try:
        # Stage 1: Download
        _update_job(job_id, "downloading", 0)
        job = db.query(Job).filter(Job.id == job_id).first()

        def dl_progress(p):
            _update_job(job_id, "downloading", p)

        if job.source_type == "url":
            meta = download_video(job.source_url, job_id, progress_callback=dl_progress)
        else:
            # File already saved during upload; extract audio and probe duration
            from services.downloader import _extract_audio, _get_duration, get_job_dir
            from pathlib import Path
            job_dir = get_job_dir(job_id)
            video_path = job_dir / "original.mp4"
            audio_path = job_dir / "audio.wav"
            _extract_audio(str(video_path), str(audio_path))
            try:
                duration = _get_duration(str(video_path))
            except Exception:
                duration = None
            meta = {
                "title": job.source_filename,
                "duration": duration,
            }

        job = db.query(Job).filter(Job.id == job_id).first()
        job.video_title = meta.get("title")
        if meta.get("duration"):
            job.video_duration_seconds = float(meta["duration"])
        db.commit()

        # Stage 2: Transcribe
        _update_job(job_id, "transcribing", 0)

        def tr_progress(p):
            _update_job(job_id, "transcribing", p)

        transcript = transcribe(job_id, progress_callback=tr_progress)

        job = db.query(Job).filter(Job.id == job_id).first()
        job.detected_language = transcript.get("language")
        db.commit()

        # Stage 3: Diarize
        _update_job(job_id, "diarizing", 0)

        def di_progress(p):
            _update_job(job_id, "diarizing", p)

        try:
            diarize(job_id, progress_callback=di_progress)
        except Exception as e:
            # Diarization is non-fatal; continue without speaker info
            _update_job(job_id, "diarizing", 100)

        # Stage 4: Analyze
        _update_job(job_id, "analyzing", 0)
        job = db.query(Job).filter(Job.id == job_id).first()

        analysis_options = {
            **options,
            "video_title": job.video_title or "",
        }

        def an_progress(p):
            _update_job(job_id, "analyzing", p)

        clip_candidates = analyze_transcript(job_id, analysis_options, progress_callback=an_progress)

        # Save clip candidates to DB
        for candidate in clip_candidates:
            duration = candidate["end_seconds"] - candidate["start_seconds"]
            tags = json.dumps(candidate.get("tags", []))
            clip = Clip(
                job_id=job_id,
                rank=candidate["rank"],
                start_seconds=candidate["start_seconds"],
                end_seconds=candidate["end_seconds"],
                duration_seconds=duration,
                clip_type=candidate["clip_type"],
                score=candidate["score"],
                reason=candidate["reason"],
                transcript_excerpt=candidate.get("transcript_excerpt", ""),
                hook_line=candidate.get("hook_line"),
                tags=tags,
                status="pending",
            )
            db.add(clip)
        db.commit()

        # Stage 5: Render clips
        _update_job(job_id, "clipping", 0)
        transcript_data = load_transcript(job_id)
        aspect_ratio = options.get("target_aspect_ratio", "9:16")

        db.expire_all()
        clips = db.query(Clip).filter(Clip.job_id == job_id).all()

        for i, clip_row in enumerate(clips):
            clip_dict = {
                "rank": clip_row.rank,
                "start_seconds": clip_row.start_seconds,
                "end_seconds": clip_row.end_seconds,
            }

            clip_row.status = "rendering"
            db.commit()

            result = None
            try:
                result = render_clip(job_id, clip_dict, aspect_ratio)
                captioned_path = result["final_clip_path"].replace("_final.mp4", "_captioned.mp4")
                burn_captions(clip_dict, transcript_data, result["final_clip_path"], captioned_path)
                clip_row.raw_clip_path = result["raw_clip_path"]
                clip_row.final_clip_path = captioned_path
                clip_row.status = "ready"
            except Exception:
                clip_row.status = "ready"
                clip_row.raw_clip_path = result["raw_clip_path"] if result else None
                clip_row.final_clip_path = result["final_clip_path"] if result else None

            db.commit()
            progress = int((i + 1) / len(clips) * 100)
            _update_job(job_id, "clipping", progress)

        _update_job(job_id, "ready", 100)

    except Exception as e:
        _update_job(job_id, "failed", 0, error=str(e))
        raise
    finally:
        db.close()


@celery.task(bind=True, name="tasks.pipeline.run_more_clips")
def run_more_clips(self, job_id: str, options: dict, excluded_clips: list):
    db = SessionLocal()
    try:
        _update_job(job_id, "analyzing", 0)
        job = db.query(Job).filter(Job.id == job_id).first()

        analysis_options = {**options, "video_title": job.video_title or ""}

        def an_progress(p):
            _update_job(job_id, "analyzing", p)

        new_candidates = find_more_clips(
            job_id, excluded_clips, analysis_options, progress_callback=an_progress
        )

        if not new_candidates:
            _update_job(job_id, "ready", 100)
            return

        # Determine next rank offset from existing clips
        existing_max_rank = db.query(Clip).filter(Clip.job_id == job_id).count()

        _update_job(job_id, "clipping", 0)
        transcript_data = load_transcript(job_id)
        aspect_ratio = options.get("target_aspect_ratio", "9:16")

        for i, candidate in enumerate(new_candidates):
            rank = existing_max_rank + i + 1
            duration = candidate["end_seconds"] - candidate["start_seconds"]
            clip_row = Clip(
                job_id=job_id,
                rank=rank,
                start_seconds=candidate["start_seconds"],
                end_seconds=candidate["end_seconds"],
                duration_seconds=duration,
                clip_type=candidate["clip_type"],
                score=candidate["score"],
                reason=candidate["reason"],
                transcript_excerpt=candidate.get("transcript_excerpt", ""),
                hook_line=candidate.get("hook_line"),
                tags=json.dumps(candidate.get("tags", [])),
                status="rendering",
            )
            db.add(clip_row)
            db.commit()

            clip_dict = {
                "rank": rank,
                "start_seconds": candidate["start_seconds"],
                "end_seconds": candidate["end_seconds"],
            }

            result = None
            try:
                result = render_clip(job_id, clip_dict, aspect_ratio)
                captioned_path = result["final_clip_path"].replace("_final.mp4", "_captioned.mp4")
                burn_captions(clip_dict, transcript_data, result["final_clip_path"], captioned_path)
                clip_row.raw_clip_path = result["raw_clip_path"]
                clip_row.final_clip_path = captioned_path
                clip_row.status = "ready"
            except Exception:
                clip_row.status = "ready"
                clip_row.raw_clip_path = result["raw_clip_path"] if result else None
                clip_row.final_clip_path = result["final_clip_path"] if result else None

            db.commit()
            _update_job(job_id, "clipping", int((i + 1) / len(new_candidates) * 100))

        _update_job(job_id, "ready", 100)

    except Exception as e:
        _update_job(job_id, "failed", 0, error=str(e))
        raise
    finally:
        db.close()
