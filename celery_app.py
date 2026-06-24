import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv
#anas is gay
load_dotenv()


def normalize_redis_url(url: str) -> str:
    if not url.startswith("rediss://"):
        return url

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("ssl_cert_reqs", "CERT_NONE")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


REDIS_URL = normalize_redis_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

celery = Celery(
    "clipforge",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks.pipeline", "tasks.cleanup"],
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    # Survive worker death (redeploys, OOM, crashes). Task is only ACKed after
    # successful completion. If worker dies mid-task, broker re-delivers it.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Give long pipelines enough graceful-shutdown time. SIGTERM → worker
    # finishes current task within this window before SIGKILL.
    worker_shutdown_timeout=120,
    beat_schedule={
        "cleanup-expired-clips": {
            "task": "cleanup_expired_free_clips",
            "schedule": crontab(hour=2, minute=0),  # Daily at 2 AM UTC
        },
        "reengagement-emails": {
            "task": "send_reengagement_emails",
            "schedule": crontab(hour=10, minute=0),  # Daily at 10 AM UTC
        },
    },
)
