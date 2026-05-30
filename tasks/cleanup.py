from datetime import datetime, timedelta
from celery import shared_task
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Clip
from services import r2


@shared_task(bind=True, name="cleanup_expired_free_clips")
def cleanup_expired_free_clips(self):
    """Delete free clips older than 3 days from R2 and mark as expired."""
    db = SessionLocal()
    try:
        three_days_ago = datetime.utcnow() - timedelta(days=3)

        expired_clips = db.query(Clip).filter(
            Clip.credit_type == "free",
            Clip.created_at < three_days_ago,
            Clip.status != "expired_deleted",
        ).all()

        deleted_count = 0
        for clip in expired_clips:
            try:
                if clip.r2_clip_key and r2.is_enabled():
                    r2.delete_object(clip.r2_clip_key)
                clip.status = "expired_deleted"
                deleted_count += 1
            except Exception as e:
                print(f"Error deleting clip {clip.id} from R2: {e}")

        db.commit()
        return {"status": "success", "deleted_count": deleted_count}

    except Exception as e:
        db.rollback()
        print(f"Cleanup task failed: {e}")
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()
