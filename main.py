import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from database import create_tables
from api.jobs import router as jobs_router
from api.clips import router as clips_router
from api.auth import router as auth_router
from api.admin import router as admin_router
from api.music import router as music_router
from api.debug import router as debug_router
from api.payments import router as payments_router
from api.subscriptions import router as subscriptions_router
from api.system import router as system_router
from api.hook_templates import router as hook_templates_router, TEMPLATES_DIR as HOOK_TEMPLATES_DIR

load_dotenv()

_DEFAULT_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://clip-smart-ai-frontend-kappa.vercel.app",
    "https://clipforge-frontend-nu.vercel.app",
]
_env_origins = os.getenv("CORS_ORIGINS", "")
CORS_ORIGINS = [o.strip() for o in _env_origins.split(",") if o.strip()] or _DEFAULT_ORIGINS

print(f"CORS_ORIGINS: {CORS_ORIGINS}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    # Reset any "exporting" clips left over from a prior worker crash. Without
    # this they stay stuck forever and the frontend polls until timeout.
    _reset_stale_exports()
    _reset_stale_jobs()
    # Purge expired video cache entries
    _purge_video_cache()
    # Seed subscription tiers
    _seed_subscription_tiers()
    # Build any new scene templates dropped in templates/hooks/_raw/ since
    # last deploy — every push restarts the server, so this is the natural
    # place to pick them up without a separate watcher process.
    _build_hook_templates()
    _sync_admin_templates()
    yield


def _build_hook_templates() -> None:
    import logging
    logger = logging.getLogger(__name__)
    try:
        from scripts.build_hook_templates import build_all
        build_all()
    except Exception:
        logger.exception("hook template build failed (continuing startup)")


def _sync_admin_templates() -> None:
    """Admin-uploaded templates (api/admin.py) live on ephemeral disk once
    built — a redeploy wipes them. Re-download overlay.png from R2 + rewrite
    meta.json from the DB row for any Template missing locally."""
    import logging
    from database import SessionLocal
    from models import Template
    from services import r2
    from api.hook_templates import TEMPLATES_DIR
    logger = logging.getLogger(__name__)
    if not r2.is_enabled():
        return
    s = SessionLocal()
    try:
        for t in s.query(Template).all():
            dest_dir = TEMPLATES_DIR / t.slug
            meta_path = dest_dir / "meta.json"
            if meta_path.exists():
                continue
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                r2.download_file(t.r2_key, str(dest_dir / "overlay.png"))
                meta_path.write_text(t.meta_json)
            except Exception:
                logger.exception("failed to resync template %s from R2", t.slug)
    except Exception:
        logger.exception("admin template resync failed (continuing startup)")
    finally:
        s.close()


def _reset_stale_exports() -> None:
    """Mark clips stuck in 'exporting' as 'failed' on startup.

    Background export thread runs in-process; if Railway restarts the worker
    mid-encode (OOM, redeploy), status never flips → permanent 'exporting'.
    """
    import datetime
    import logging
    from database import SessionLocal
    from models import Clip
    logger = logging.getLogger(__name__)
    s = SessionLocal()
    try:
        # Anything older than 5 min in 'exporting' is presumed dead. New
        # legitimate exports started after restart get their own status row.
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
        # Some schemas don't have updated_at — fall back to created_at filter
        # being permissive. We mainly want a one-shot cleanup on startup.
        stuck = s.query(Clip).filter(Clip.status == "exporting").all()
        if not stuck:
            return
        count = 0
        for row in stuck:
            ts = getattr(row, "updated_at", None) or row.created_at
            if ts and ts > cutoff:
                continue  # too fresh to be stuck
            row.status = "failed"
            row.error_message = "export interrupted (worker restart)"
            count += 1
        if count:
            s.commit()
            logger.warning("startup sweeper: reset %d stuck 'exporting' clips", count)
    except Exception as exc:
        logger.exception("startup sweeper failed: %s", exc)
    finally:
        s.close()


def _reset_stale_jobs() -> None:
    """Mark jobs stuck in active statuses as failed on startup.

    Celery re-queues tasks on worker restart (task_acks_late=True), but if
    Redis flushed or the worker died permanently the job stays in-flight forever.
    30-min cutoff: any legitimate job still alive after restart will re-report
    its own status update quickly.
    """
    import datetime
    import logging
    from database import SessionLocal
    from models import Job
    logger = logging.getLogger(__name__)
    active = ["pending", "downloading", "transcribing", "diarizing", "analyzing", "clipping"]
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=30)
    s = SessionLocal()
    try:
        stuck = s.query(Job).filter(
            Job.status.in_(active),
            Job.created_at < cutoff,
        ).all()
        if not stuck:
            return
        for job in stuck:
            job.status = "failed"
            job.error_message = "Job interrupted (worker restart)"
        s.commit()
        logger.warning("startup sweeper: reset %d stuck jobs", len(stuck))
    except Exception as exc:
        logger.exception("job sweeper failed: %s", exc)
    finally:
        s.close()


def _purge_video_cache() -> None:
    """Delete expired cached_videos rows on startup."""
    import logging
    from database import SessionLocal
    from services.video_cache import purge_expired

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        n = purge_expired(db)
        if n:
            logger.info("video cache: purged %d expired entries", n)
    except Exception as exc:
        logger.exception("video cache purge failed: %s", exc)
    finally:
        db.close()


def _seed_subscription_tiers() -> None:
    """Seed 4 subscription tiers on startup (idempotent)."""
    import logging
    from database import SessionLocal
    from services.subscriptions import seed_subscription_tiers

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        seed_subscription_tiers(db)
        logger.info("Subscription tiers seeded")
    except Exception as exc:
        logger.exception("Failed to seed subscription tiers: %s", exc)
    finally:
        db.close()


app = FastAPI(title="ClipForge API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

storage_path = os.getenv("STORAGE_PATH", "./storage")
os.makedirs(storage_path, exist_ok=True)

app.mount("/storage", StaticFiles(directory=storage_path), name="storage")

HOOK_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/hook-templates-static", StaticFiles(directory=str(HOOK_TEMPLATES_DIR)), name="hook-templates-static")

@app.get("/")
def root():
    return {"status": "ok", "service": "clipforge-api"}


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(auth_router, prefix="/api")
app.include_router(system_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(clips_router, prefix="/api")
app.include_router(music_router, prefix="/api")
app.include_router(payments_router, prefix="/api")
app.include_router(subscriptions_router, prefix="/api")
app.include_router(debug_router, prefix="/api")
app.include_router(hook_templates_router, prefix="/api")
