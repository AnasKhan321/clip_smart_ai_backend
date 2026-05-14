import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from celery import Celery
from dotenv import load_dotenv

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
    include=["tasks.pipeline"],
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
)
