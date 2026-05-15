import os
import logging
from threading import Thread
from uuid import uuid4
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from database import get_db, SessionLocal
from models import Job, Clip, User
from schemas import JobOut, JobCreate, ClipOut
from tasks.pipeline import run_full_pipeline, run_more_clips
from auth import get_current_user
from services.credits import deduct, cost_for_job
from services.media_tools import MediaToolMissingError, ffmpeg_path, ffprobe_path

router = APIRouter()
logger = logging.getLogger(__name__)


def run_task_in_background(task, *args):
    if os.getenv("RUN_JOBS_INLINE", "false").lower() in ("1", "true", "yes", "on"):
        Thread(target=lambda: task.apply(args=args, throw=True), daemon=True).start()
        return {"mode": "inline"}

    async_result = task.delay(*args)
    return {"mode": "celery", "task_id": async_result.id}


@router.post("/jobs")
async def create_job(
    source_url: Optional[str] = Form(None),
    max_clips: int = Form(5),
    clip_types: str = Form("controversy,hook_intro,quotable,shocking_stat,myth_bust"),
    min_clip_duration: int = Form(20),
    max_clip_duration: int = Form(90),
    target_aspect_ratio: str = Form("9:16"),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not source_url and not file:
        raise HTTPException(status_code=400, detail="Provide either source_url or upload a file")

    try:
        ffmpeg_path()
        ffprobe_path()
    except MediaToolMissingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Charge credits upfront. Raises 402 if insufficient (no-op in dev).
    cost = cost_for_job(max_clips)

    max_mb = int(os.getenv("MAX_FILE_SIZE_MB", 2000))

    job_id = str(uuid4())
    source_type = "url" if source_url else "upload"
    source_filename = None

    if file:
        content = await file.read()
        size_mb = len(content) / (1024 * 1024)
        if size_mb > max_mb:
            raise HTTPException(status_code=413, detail=f"File exceeds {max_mb}MB limit")

        storage = os.getenv("STORAGE_PATH", "./storage")
        job_dir = os.path.join(storage, "jobs", job_id)
        os.makedirs(job_dir, exist_ok=True)
        video_path = os.path.join(job_dir, "original.mp4")
        with open(video_path, "wb") as f:
            f.write(content)
        source_filename = file.filename

    job = Job(
        id=job_id,
        user_id=user.id,
        source_url=source_url,
        source_filename=source_filename,
        source_type=source_type,
        status="pending",
    )
    db.add(job)
    db.flush()

    deduct(db, user, cost, job_id=job_id, note=f"Job {job_id} ({max_clips} clips)")
    db.commit()

    options = {
        "source_url": source_url,
        "max_clips": max_clips,
        "clip_types": [t.strip() for t in clip_types.split(",")],
        "min_clip_duration": min_clip_duration,
        "max_clip_duration": max_clip_duration,
        "target_aspect_ratio": target_aspect_ratio,
    }

    try:
        dispatch = run_task_in_background(run_full_pipeline, job_id, options)
    except Exception as exc:
        logger.exception("Failed to dispatch job %s", job_id)
        job.status = "failed"
        job.error_message = f"Failed to dispatch job: {exc}"
        db.commit()
        raise HTTPException(status_code=503, detail=job.error_message) from exc

    return {
        "job_id": job_id,
        "status": "pending",
        "dispatch": dispatch,
        "message": f"Job queued. Poll /api/jobs/{job_id} for progress.",
    }

@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    jobs = (
        db.query(Job)
        .filter(Job.user_id == user.id)
        .order_by(Job.created_at.desc())
        .all()
    )
    out = []
    for j in jobs:
        clips = db.query(Clip).filter(Clip.job_id == j.id).order_by(Clip.rank).all()
        out.append(JobOut(
            job_id=j.id, status=j.status, stage_progress=j.stage_progress,
            source_url=j.source_url, source_filename=j.source_filename, source_type=j.source_type,
            detected_language=j.detected_language, detected_topic=j.detected_topic,
            video_duration_seconds=j.video_duration_seconds, video_title=j.video_title,
            error_message=j.error_message, clips=clips,
            created_at=j.created_at, completed_at=j.completed_at,
        ))
    return out


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id and job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your job")

    clips = db.query(Clip).filter(Clip.job_id == job_id).order_by(Clip.rank).all()

    return JobOut(
        job_id=job.id,
        status=job.status,
        stage_progress=job.stage_progress,
        source_url=job.source_url,
        source_filename=job.source_filename,
        source_type=job.source_type,
        detected_language=job.detected_language,
        detected_topic=job.detected_topic,
        video_duration_seconds=job.video_duration_seconds,
        video_title=job.video_title,
        error_message=job.error_message,
        clips=clips,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


@router.post("/jobs/{job_id}/more-clips")
def more_clips(
    job_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id and job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your job")
    if job.status not in ("ready", "failed"):
        raise HTTPException(status_code=400, detail="Job must be complete before requesting more clips")

    max_clips = int(body.get("max_clips", 5))

    # Charge credits for the additional clips before dispatching.
    # Raises 402 in prod if balance is short; no-op in dev (logged as bypass).
    cost = cost_for_job(max_clips)
    deduct(db, user, cost, job_id=job_id,
           note=f"More clips: {max_clips} additional")

    existing_clips = db.query(Clip).filter(Clip.job_id == job_id).all()
    excluded = [
        {"start_seconds": c.start_seconds, "end_seconds": c.end_seconds}
        for c in existing_clips
    ]

    options = {
        "source_url": job.source_url,
        "max_clips": max_clips,
        "clip_types": body.get("clip_types", ["controversy", "hook_intro", "quotable", "shocking_stat", "myth_bust"]),
        "min_clip_duration": body.get("min_clip_duration", 20),
        "max_clip_duration": body.get("max_clip_duration", 90),
        "target_aspect_ratio": body.get("target_aspect_ratio", "9:16"),
    }

    job.status = "analyzing"
    job.stage_progress = 0
    job.error_message = None
    db.commit()

    dispatch = run_task_in_background(run_more_clips, job_id, options, excluded)
    return {"job_id": job_id, "status": "analyzing",
            "excluded_clips": len(excluded), "credits_charged": cost,
            "dispatch": dispatch}


class ManualClipIn(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    label: Optional[str] = Field(None, max_length=200)


def _render_manual_clip_bg(clip_id: str):
    db = SessionLocal()
    try:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip:
            return
        from services.editor import render_and_caption_clip
        from services.transcriber import load_transcript

        clip_dict = {
            "rank": clip.rank,
            "start_seconds": clip.start_seconds,
            "end_seconds": clip.end_seconds,
            "user_start_seconds": None,
            "user_end_seconds": None,
        }

        job = db.query(Job).filter(Job.id == clip.job_id).first()
        source_dims = (job.source_width, job.source_height) \
            if (job and job.source_width and job.source_height) else None
        try:
            transcript = load_transcript(clip.job_id)
        except FileNotFoundError:
            transcript = None

        result = render_and_caption_clip(
            clip.job_id, clip_dict,
            aspect_ratio="9:16",
            include_captions=True,
            transcript=transcript,
            source_dims=source_dims,
            profile="preview",
        )
        if result["error"]:
            clip.status = "failed"
            clip.error_message = result["error"]
            logger.warning("manual clip %s render failed: %s",
                           clip_id, result["error"])
        else:
            clip.final_clip_path = result["final_clip_path"]
            clip.raw_clip_path = None
            clip.status = "ready"
        db.commit()
    finally:
        db.close()


@router.post("/jobs/{job_id}/manual-clip", response_model=ClipOut)
def create_manual_clip(
    job_id: str,
    body: ManualClipIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id and job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your job")
    if body.end_seconds <= body.start_seconds:
        raise HTTPException(status_code=400, detail="end_seconds must be greater than start_seconds")
    if body.end_seconds - body.start_seconds < 1:
        raise HTTPException(status_code=400, detail="Clip must be at least 1 second long")

    max_rank = db.query(Clip).filter(Clip.job_id == job_id).count()
    rank = max_rank + 1

    clip = Clip(
        id=str(uuid4()),
        job_id=job_id,
        rank=rank,
        start_seconds=body.start_seconds,
        end_seconds=body.end_seconds,
        clip_type="manual",
        reason=body.label or "Manual clip",
        status="rendering",
        user_approved=True,
    )
    db.add(clip)
    db.commit()
    db.refresh(clip)

    Thread(target=_render_manual_clip_bg, args=(clip.id,), daemon=True).start()

    return clip


@router.post("/jobs/{job_id}/regenerate")
def regenerate_job(
    job_id: str,
    body: JobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import pathlib
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id and job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your job")
    if job.status not in ("ready", "failed"):
        raise HTTPException(status_code=400, detail="Job must be complete before regenerating")

    storage = os.getenv("STORAGE_PATH", "./storage")
    analysis_cache = pathlib.Path(storage) / "jobs" / job_id / "analysis.json"
    analysis_cache.unlink(missing_ok=True)

    # Delete existing clips
    db.query(Clip).filter(Clip.job_id == job_id).delete()
    job.status = "analyzing"
    job.stage_progress = 0
    job.error_message = None
    db.commit()

    options = {
        "source_url": job.source_url,
        "max_clips": body.max_clips,
        "clip_types": body.clip_types,
        "min_clip_duration": body.min_clip_duration,
        "max_clip_duration": body.max_clip_duration,
        "target_aspect_ratio": body.target_aspect_ratio,
        "_skip_to_analyze": True,
    }

    run_task_in_background(run_full_pipeline, job_id, options)
    return {"job_id": job_id, "status": "analyzing"}
