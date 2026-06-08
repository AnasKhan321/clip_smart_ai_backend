"""Development-only debugging endpoints."""
import os
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from celery_app import REDIS_URL, celery
from database import get_db
from models import Job
from services.media_tools import media_tools_status

router = APIRouter(prefix="/debug", tags=["debug"])


def require_debug_enabled():
    if os.getenv("DEBUG", "false").lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=404, detail="Not found")


def safe_redis_url() -> dict:
    parts = urlsplit(REDIS_URL)
    return {
        "scheme": parts.scheme,
        "host": parts.hostname,
        "port": parts.port,
        "database": parts.path.lstrip("/") or None,
        "has_ssl_cert_reqs": "ssl_cert_reqs=" in parts.query,
    }


@router.get("/health")
def debug_health(
    check_network: bool = Query(False),
    _: None = Depends(require_debug_enabled),
):
    redis = safe_redis_url()
    run_jobs_inline = os.getenv("RUN_JOBS_INLINE", "false")
    dns_ok = None
    dns_error = None

    worker_ping = None
    worker_error = None
    if not check_network:
        worker_error = "Skipped. Pass check_network=true to test Redis/Celery connectivity."
    elif run_jobs_inline.lower() in ("1", "true", "yes", "on"):
        worker_error = "Skipped because RUN_JOBS_INLINE is enabled."
    else:
        try:
            worker_ping = celery.control.inspect(timeout=1).ping()
        except Exception as exc:
            worker_error = str(exc)

    return {
        "debug": True,
        "run_jobs_inline": run_jobs_inline,
        "redis": redis,
        "redis_dns_ok": dns_ok,
        "redis_dns_error": dns_error,
        "celery_worker_ping": worker_ping,
        "celery_worker_error": worker_error,
        "media_tools": media_tools_status(),
    }


@router.get("/jobs/{job_id}")
def debug_job(job_id: str, db: Session = Depends(get_db), _: None = Depends(require_debug_enabled)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id": job.id,
        "status": job.status,
        "stage_progress": job.stage_progress,
        "error_message": job.error_message,
        "source_type": job.source_type,
        "source_url": job.source_url,
        "source_filename": job.source_filename,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
    }


@router.post("/test-download")
def test_download(
    url: str = Query(..., description="YouTube URL to test"),
    _: None = Depends(require_debug_enabled),
):
    """Download via Celery worker and report quality/speed. No credits deducted."""
    from tasks.pipeline import test_download_task
    task = test_download_task.apply_async(args=[url])
    try:
        result = task.get(timeout=300)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/jobs/{job_id}/rerun")
def rerun_job(job_id: str, db: Session = Depends(get_db), _: None = Depends(require_debug_enabled)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = "pending"
    job.stage_progress = 0
    job.error_message = None
    job.completed_at = None
    db.commit()

    options = {
        "source_url": job.source_url,
        "max_clips": 5,
        "clip_types": ["controversy", "hook_intro", "quotable", "shocking_stat", "myth_bust"],
        "min_clip_duration": 20,
        "max_clip_duration": 90,
        "target_aspect_ratio": "9:16",
    }

    from api.jobs import run_task_in_background
    from tasks.pipeline import run_full_pipeline

    dispatch = run_task_in_background(run_full_pipeline, job.id, options)
    return {"job_id": job.id, "status": job.status, "dispatch": dispatch}
