"""Cloudflare R2 storage helpers.

Retry & self-heal strategy
~~~~~~~~~~~~~~~~~~~~~~~~~~
``upload_in_background`` retries up to 3× with exponential back-off.
On final failure it calls an optional ``on_failure(key)`` callback so the
caller can clear the stale ``r2_clip_key`` from the DB row.

R2 = S3-compatible object store. Used as durable layer:
  - browser uploads original video direct-to-R2 (multipart presign, bypasses backend)
  - worker pulls from R2 to local scratch for ffmpeg/whisper
  - rendered clips pushed back to R2
  - clips served via R2 public custom domain (CDN edge, zero egress)

Env:
  R2_ACCOUNT_ID         Cloudflare account id
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_BUCKET             bucket name
  R2_PUBLIC_URL         optional, e.g. https://cdn.example.com (if bucket public)
  R2_REGION             defaults to "auto"
  R2_SIGNED_URL_TTL     seconds, default 3600
"""
from __future__ import annotations

import os
import time
import threading
import logging
from pathlib import Path
from typing import Callable, Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

_client = None
_client_lock = threading.Lock()

_MULTIPART_THRESHOLD = 16 * 1024 * 1024  # 16 MB
_MULTIPART_CHUNKSIZE = 8 * 1024 * 1024   # 8 MB parts


def is_enabled() -> bool:
    return bool(os.getenv("R2_BUCKET") and os.getenv("R2_ACCOUNT_ID")
                and os.getenv("R2_ACCESS_KEY_ID") and os.getenv("R2_SECRET_ACCESS_KEY"))


def bucket() -> str:
    return os.environ["R2_BUCKET"]


def get_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        account = os.environ["R2_ACCOUNT_ID"]
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name=os.getenv("R2_REGION", "auto"),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return _client


# ── Key helpers ─────────────────────────────────────────────────────────────

def source_key(job_id: str, ext: str = "mp4") -> str:
    return f"jobs/{job_id}/original.{ext}"


def clip_key(job_id: str, rank: int) -> str:
    return f"jobs/{job_id}/clips/clip_{rank:03d}.mp4"


# ── Presigned multipart upload ──────────────────────────────────────────────

def create_multipart_upload(key: str, content_type: str = "video/mp4") -> dict:
    """Initiate multipart upload, return {upload_id, key}."""
    resp = get_client().create_multipart_upload(
        Bucket=bucket(), Key=key, ContentType=content_type,
    )
    return {"upload_id": resp["UploadId"], "key": key}


def presign_part_urls(key: str, upload_id: str, part_count: int,
                      ttl: int = 3600) -> list[dict]:
    """Generate presigned PUT URLs for each part. Client uploads parts in parallel."""
    cli = get_client()
    out = []
    for i in range(1, part_count + 1):
        url = cli.generate_presigned_url(
            "upload_part",
            Params={"Bucket": bucket(), "Key": key,
                    "UploadId": upload_id, "PartNumber": i},
            ExpiresIn=ttl,
        )
        out.append({"part_number": i, "url": url})
    return out


def complete_multipart_upload(key: str, upload_id: str, parts: list[dict]) -> dict:
    """parts: [{part_number, etag}] — order doesn't matter, sorted here."""
    parts_sorted = sorted(parts, key=lambda p: p["part_number"])
    return get_client().complete_multipart_upload(
        Bucket=bucket(), Key=key, UploadId=upload_id,
        MultipartUpload={"Parts": [
            {"PartNumber": p["part_number"], "ETag": p["etag"]}
            for p in parts_sorted
        ]},
    )


def abort_multipart_upload(key: str, upload_id: str) -> None:
    try:
        get_client().abort_multipart_upload(
            Bucket=bucket(), Key=key, UploadId=upload_id,
        )
    except ClientError:
        pass


# ── Simple presign (small files / single-shot) ──────────────────────────────

def presign_put(key: str, content_type: str = "application/octet-stream",
                ttl: int = 3600) -> str:
    return get_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=ttl,
    )


# ── Public / signed read URL ────────────────────────────────────────────────

def object_url(key: str, ttl: Optional[int] = None,
               download_filename: Optional[str] = None) -> str:
    """Public URL via R2_PUBLIC_URL if set, else signed URL.

    download_filename: when set, presigned URL includes
    Content-Disposition=attachment so browser saves the file instead of
    rendering it inline. Ignored on the R2_PUBLIC_URL path (public custom
    domain serves whatever the bucket has, no per-request overrides).
    """
    pub = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
    if pub and not download_filename:
        return f"{pub}/{key}"
    params = {"Bucket": bucket(), "Key": key}
    if download_filename:
        # Quote-escape any quotes in filename to keep header well-formed.
        safe = download_filename.replace('"', '')
        params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
    return get_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=ttl or int(os.getenv("R2_SIGNED_URL_TTL", "3600")),
    )


# ── Upload / download (server-side, used by worker) ─────────────────────────

def upload_file(local_path: str, key: str, content_type: str = "video/mp4") -> str:
    """Multipart upload local file → R2. Returns key."""
    from boto3.s3.transfer import TransferConfig
    cfg = TransferConfig(
        multipart_threshold=_MULTIPART_THRESHOLD,
        multipart_chunksize=_MULTIPART_CHUNKSIZE,
        max_concurrency=8,
        use_threads=True,
    )
    get_client().upload_file(
        Filename=local_path, Bucket=bucket(), Key=key,
        ExtraArgs={"ContentType": content_type},
        Config=cfg,
    )
    return key


def download_file(key: str, local_path: str) -> str:
    """Multipart parallel download R2 → local file."""
    from boto3.s3.transfer import TransferConfig
    cfg = TransferConfig(
        multipart_threshold=_MULTIPART_THRESHOLD,
        multipart_chunksize=_MULTIPART_CHUNKSIZE,
        max_concurrency=8,
        use_threads=True,
    )
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    get_client().download_file(
        Bucket=bucket(), Key=key, Filename=local_path, Config=cfg,
    )
    return local_path


def object_exists(key: str) -> bool:
    try:
        get_client().head_object(Bucket=bucket(), Key=key)
        return True
    except ClientError:
        return False


def head_size(key: str) -> int:
    return int(get_client().head_object(Bucket=bucket(), Key=key)["ContentLength"])


def delete_prefix(prefix: str) -> int:
    """Delete all keys under a prefix. Returns count."""
    cli = get_client()
    n = 0
    paginator = cli.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
        objs = page.get("Contents") or []
        if not objs:
            continue
        cli.delete_objects(
            Bucket=bucket(),
            Delete={"Objects": [{"Key": o["Key"]} for o in objs]},
        )
        n += len(objs)
    return n


# ── Background async push (fire-and-forget from worker) ─────────────────────

_bg_logger = logging.getLogger(__name__)

_UPLOAD_MAX_RETRIES = 3
_UPLOAD_BACKOFF_BASE = 2  # seconds; retry 1 → 2s, 2 → 4s, 3 → 8s


def upload_in_background(
    local_path: str,
    key: str,
    content_type: str = "video/mp4",
    on_failure: "Callable[[str], None] | None" = None,
) -> threading.Thread:
    """Upload file to R2 in a daemon thread with retry + optional cleanup.

    Args:
        on_failure: called with *key* after all retries are exhausted.
                    Use it to clear the stale ``r2_clip_key`` from the DB.
    """
    def _run():
        last_exc: Exception | None = None
        for attempt in range(1, _UPLOAD_MAX_RETRIES + 1):
            try:
                upload_file(local_path, key, content_type)
                _bg_logger.info("[r2] background upload OK %s (attempt %d)",
                                key, attempt)
                return  # success
            except Exception as exc:
                last_exc = exc
                _bg_logger.warning(
                    "[r2] background upload attempt %d/%d failed for %s: %s",
                    attempt, _UPLOAD_MAX_RETRIES, key, exc,
                )
                if attempt < _UPLOAD_MAX_RETRIES:
                    time.sleep(_UPLOAD_BACKOFF_BASE ** attempt)

        # All retries exhausted
        _bg_logger.error(
            "[r2] background upload FAILED permanently for %s: %s",
            key, last_exc,
        )
        if on_failure:
            try:
                on_failure(key)
            except Exception as cb_exc:
                _bg_logger.error(
                    "[r2] on_failure callback error for %s: %s", key, cb_exc,
                )

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
