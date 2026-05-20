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
from services import r2

router = APIRouter()
logger = logging.getLogger(__name__)


class _SourceInvalid(Exception):
    pass


def _validate_r2_source(key: str) -> None:
    """ffprobe the R2 object via presigned URL. Reject if no audio stream,
    if probe fails outright, or if audio/video durations diverge wildly
    (telltale sign of a broken upload / busted encoder).

    Streams probe over the network; does not download the file.
    """
    import json as _json
    import subprocess as _sp

    url = r2.object_url(key, ttl=1800)
    try:
        proc = _sp.run(
            [ffprobe_path(),
             "-v", "error",
             "-rw_timeout", "30000000",      # 30s per network read
             "-analyzeduration", "20000000", # 20s of stream
             "-probesize", "50000000",       # 50 MB probed (catches non-faststart moov)
             "-print_format", "json",
             "-show_streams", "-show_format", url],
            capture_output=True, timeout=180, check=False,
        )
    except _sp.TimeoutExpired:
        logger.warning("[validate] ffprobe timeout for %s — skipping validation", key)
        return

    # Trust JSON output over exit code: ffprobe can exit non-zero on benign
    # read warnings while still emitting full stream metadata. Only fail if
    # we got nothing parseable AND ffprobe returned an error.
    try:
        meta = _json.loads(proc.stdout or "{}")
    except _json.JSONDecodeError:
        meta = {}

    if not meta.get("streams"):
        if proc.returncode != 0:
            raise _SourceInvalid(
                "Source file is corrupt or not a supported video format. "
                "Re-encode with ffmpeg and retry."
            )
        # No streams + zero exit = exotic format; let pipeline try.
        logger.warning("[validate] no streams parsed for %s — skipping validation", key)
        return

    streams = meta.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if s.get("codec_type") == "video"]

    if not audio:
        raise _SourceInvalid("Source has no audio track — nothing to transcribe.")

    def _dur(s):
        try:
            return float(s.get("duration") or 0)
        except (TypeError, ValueError):
            return 0.0

    a_dur = max((_dur(s) for s in audio), default=0.0)
    v_dur = max((_dur(s) for s in video), default=0.0)
    if v_dur > 0 and a_dur > 0:
        ratio = max(a_dur, v_dur) / min(a_dur, v_dur)
        if ratio > 3.0:
            raise _SourceInvalid(
                f"Source is malformed: audio ({a_dur:.0f}s) and video "
                f"({v_dur:.0f}s) durations do not match. Re-encode and retry."
            )


def run_task_in_background(task, *args):
    if os.getenv("RUN_JOBS_INLINE", "false").lower() in ("1", "true", "yes", "on"):
        Thread(target=lambda: task.apply(args=args, throw=True), daemon=True).start()
        return {"mode": "inline"}

    async_result = task.delay(*args)
    return {"mode": "celery", "task_id": async_result.id}


# ── R2 direct-upload flow ───────────────────────────────────────────────────
# Browser uploads original video direct-to-R2 via presigned multipart PUTs.
# Backend never touches the bytes. Big win on Railway: no proxy bottleneck,
# no ephemeral disk used for upload, no request-body memory spike.

class PresignUploadIn(BaseModel):
    filename: str
    content_type: str = "video/mp4"
    size_bytes: int = Field(gt=0)


@router.post("/jobs/presign-upload")
def presign_upload(
    body: PresignUploadIn,
    user: User = Depends(get_current_user),
):
    if not r2.is_enabled():
        raise HTTPException(status_code=503,
                            detail="R2 storage not configured on server")

    max_mb = int(os.getenv("MAX_FILE_SIZE_MB", 2000))
    if body.size_bytes > max_mb * 1024 * 1024:
        raise HTTPException(status_code=413,
                            detail=f"File exceeds {max_mb}MB limit")

    chunk_size = 8 * 1024 * 1024  # 8 MB parts
    part_count = max(1, (body.size_bytes + chunk_size - 1) // chunk_size)
    if part_count > 10000:  # S3 hard limit
        raise HTTPException(status_code=413,
                            detail="File too large for multipart upload")

    job_id = str(uuid4())
    key = r2.source_key(job_id, ext="mp4")

    init = r2.create_multipart_upload(key, content_type=body.content_type)
    parts = r2.presign_part_urls(key, init["upload_id"], part_count, ttl=3600)

    return {
        "job_id": job_id,
        "key": key,
        "upload_id": init["upload_id"],
        "part_size": chunk_size,
        "part_count": part_count,
        "parts": parts,  # [{part_number, url}]
    }


class CompletePartIn(BaseModel):
    part_number: int
    etag: str


class JobFromR2In(BaseModel):
    job_id: str
    key: str
    upload_id: str
    parts: list[CompletePartIn]
    filename: str
    max_clips: int = 5
    clip_types: list[str] = Field(default_factory=lambda: [
        "controversy", "hook_intro", "quotable", "shocking_stat", "myth_bust"])
    min_clip_duration: int = 20
    max_clip_duration: int = 90
    target_aspect_ratio: str = "9:16"
    custom_prompt: Optional[str] = None


class AbortUploadIn(BaseModel):
    key: str
    upload_id: str


@router.post("/jobs/abort-upload")
def abort_upload(body: AbortUploadIn, user: User = Depends(get_current_user)):
    if r2.is_enabled():
        r2.abort_multipart_upload(body.key, body.upload_id)
    return {"ok": True}


@router.post("/jobs/from-r2")
def create_job_from_r2(
    body: JobFromR2In,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Called after browser finishes uploading all parts to R2.

    Completes the multipart upload, creates the Job row pointing at the
    R2 key, charges credits, and dispatches the pipeline.
    """
    if not r2.is_enabled():
        raise HTTPException(status_code=503, detail="R2 not configured")

    try:
        ffmpeg_path(); ffprobe_path()
    except MediaToolMissingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        r2.complete_multipart_upload(
            body.key, body.upload_id,
            [{"part_number": p.part_number, "etag": p.etag} for p in body.parts],
        )
    except Exception as exc:
        r2.abort_multipart_upload(body.key, body.upload_id)
        raise HTTPException(status_code=400,
                            detail=f"Failed to finalize R2 upload: {exc}")

    try:
        _validate_r2_source(body.key)
    except _SourceInvalid as exc:
        # Delete the broken upload so it doesn't eat R2 quota.
        try:
            r2.get_client().delete_object(Bucket=r2.bucket(), Key=body.key)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(exc))

    cost = cost_for_job(body.max_clips)
    job = Job(
        id=body.job_id,
        user_id=user.id,
        source_url=None,
        source_filename=body.filename,
        source_type="upload",
        r2_source_key=body.key,
        status="pending",
    )
    db.add(job)
    db.flush()
    deduct(db, user, cost, job_id=body.job_id,
           note=f"Job {body.job_id} ({body.max_clips} clips)")
    db.commit()

    options = {
        "source_url": None,
        "max_clips": body.max_clips,
        "clip_types": body.clip_types,
        "min_clip_duration": body.min_clip_duration,
        "max_clip_duration": body.max_clip_duration,
        "target_aspect_ratio": body.target_aspect_ratio,
    }
    _cp = (body.custom_prompt or "").strip()
    if _cp:
        options["custom_prompt"] = _cp
    dispatch = run_task_in_background(run_full_pipeline, body.job_id, options)
    return {"job_id": body.job_id, "status": "pending", "dispatch": dispatch}


@router.post("/jobs")
async def create_job(
    source_url: Optional[str] = Form(None),
    max_clips: int = Form(5),
    clip_types: str = Form("controversy,hook_intro,quotable,shocking_stat,myth_bust"),
    min_clip_duration: int = Form(20),
    max_clip_duration: int = Form(90),
    target_aspect_ratio: str = Form("9:16"),
    custom_prompt: Optional[str] = Form(None),
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
        "clip_types": [t.strip() for t in clip_types.split(",") if t.strip()],
        "min_clip_duration": min_clip_duration,
        "max_clip_duration": max_clip_duration,
        "target_aspect_ratio": target_aspect_ratio,
    }
    _cp = (custom_prompt or "").strip()
    if _cp:
        options["custom_prompt"] = _cp

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
    from api.clips import serialize_clip
    out = []
    for j in jobs:
        clips = db.query(Clip).filter(Clip.job_id == j.id).order_by(Clip.rank).all()
        out.append(JobOut(
            job_id=j.id, status=j.status, stage_progress=j.stage_progress,
            source_url=j.source_url, source_filename=j.source_filename, source_type=j.source_type,
            detected_language=j.detected_language, detected_topic=j.detected_topic,
            video_duration_seconds=j.video_duration_seconds, video_title=j.video_title,
            error_message=j.error_message,
            clips=[serialize_clip(c) for c in clips],
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

    from api.clips import serialize_clip
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
        clips=[serialize_clip(c) for c in clips],
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
    custom_prompt = (body.get("custom_prompt") or "").strip()
    if custom_prompt:
        options["custom_prompt"] = custom_prompt

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

        from services.exporter import _ensure_local_source
        _ensure_local_source(clip.job_id)

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
            if r2.is_enabled() and result["final_clip_path"]:
                key = r2.clip_key(clip.job_id, clip.rank)
                r2.upload_file(result["final_clip_path"], key)
                if r2.object_exists(key):
                    clip.r2_clip_key = key
                    clip.status = "ready"
                else:
                    clip.status = "failed"
                    clip.error_message = "R2 upload verification failed"
            else:
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
        duration_seconds=body.end_seconds - body.start_seconds,
        clip_type="manual",
        reason=body.label or "Manual clip",
        status="rendering",
        user_approved=True,
        score=0.5,
        transcript_excerpt="",
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
