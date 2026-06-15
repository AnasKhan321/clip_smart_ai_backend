import os
import shutil
from datetime import datetime, timedelta
from celery import shared_task
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Clip, Job, User, Payment, CreditTransaction
from services import r2
from services.downloader import get_job_dir


def _remove_local_file(path: str | None) -> None:
    """Delete a local file if it exists, ignoring missing paths."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        print(f"Error removing local file {path}: {e}")


@shared_task(bind=True, name="cleanup_expired_free_clips")
def cleanup_expired_free_clips(self):
    """Delete free clips older than 3 days from R2 + local disk, mark expired.

    Once every clip of a job is expired and the owner is not a paid user, the
    whole job (source video + clips + exports) is purged from R2 and local disk.
    """
    db = SessionLocal()
    try:
        three_days_ago = datetime.utcnow() - timedelta(days=3)

        paid_user_ids = set()
        for u in db.query(User.id).filter(User.subscription_tier_id.isnot(None)).all():
            paid_user_ids.add(u.id)
        for p in db.query(Payment.user_id).filter(Payment.status == "success").all():
            paid_user_ids.add(p.user_id)
        for ct in db.query(CreditTransaction.user_id).filter(CreditTransaction.kind == "admin_grant").all():
            paid_user_ids.add(ct.user_id)

        expired_clips = db.query(Clip).filter(
            Clip.credit_type == "free",
            Clip.created_at < three_days_ago,
            Clip.status != "expired_deleted",
        ).all()

        deleted_count = 0
        affected_job_ids = set()
        for clip in expired_clips:
            job = db.query(Job).filter(Job.id == clip.job_id).first()
            if job and job.user_id in paid_user_ids:
                clip.credit_type = "paid"
                continue

            try:
                if clip.r2_clip_key and r2.is_enabled():
                    r2.delete_object(clip.r2_clip_key)
                # Remove local rendered/exported files for this clip.
                _remove_local_file(clip.raw_clip_path)
                _remove_local_file(clip.final_clip_path)
                if clip.final_clip_path:
                    base, ext = os.path.splitext(clip.final_clip_path)
                    _remove_local_file(f"{base}_export{ext}")
                clip.status = "expired_deleted"
                deleted_count += 1
                affected_job_ids.add(clip.job_id)
            except Exception as e:
                print(f"Error deleting clip {clip.id}: {e}")

        # Flush so the "all clips expired" check below sees the new statuses.
        db.flush()

        # Purge source video + leftover R2/local data for fully-expired jobs.
        purged_jobs = 0
        for job_id in affected_job_ids:
            try:
                remaining = db.query(Clip).filter(
                    Clip.job_id == job_id,
                    Clip.status != "expired_deleted",
                ).count()
                if remaining > 0:
                    continue

                if r2.is_enabled():
                    r2.delete_prefix(f"jobs/{job_id}/")
                shutil.rmtree(get_job_dir(job_id), ignore_errors=True)

                job = db.query(Job).filter(Job.id == job_id).first()
                if job:
                    job.status = "expired_deleted"
                purged_jobs += 1
            except Exception as e:
                print(f"Error purging job {job_id}: {e}")

        db.commit()
        return {"status": "success", "deleted_count": deleted_count, "purged_jobs": purged_jobs}

    except Exception as e:
        db.rollback()
        print(f"Cleanup task failed: {e}")
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()
