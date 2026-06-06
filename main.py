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
    # Seed subscription tiers
    _seed_subscription_tiers()
    yield


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

@app.get("/")
def root():
    return {"status": "ok", "service": "clipforge-api"}


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(clips_router, prefix="/api")
app.include_router(music_router, prefix="/api")
app.include_router(payments_router, prefix="/api")
app.include_router(subscriptions_router, prefix="/api")
app.include_router(debug_router, prefix="/api")
