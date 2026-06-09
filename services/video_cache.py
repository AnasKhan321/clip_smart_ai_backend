"""
Video cache service.

Caches downloaded YouTube videos in R2 by video ID.
- Free / topup-only users  → 720p
- Active subscribers        → 1080p
Cache TTL: 30 days, extended on every use.
"""

from datetime import datetime, timedelta
from pathlib import Path

from models import CachedVideo, User

CACHE_TTL_DAYS = 30


def _extract_video_id(url: str) -> str:
    import re
    for pat in [
        r"[?&]v=([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"/shorts/([a-zA-Z0-9_-]{11})",
    ]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""


def required_quality(user: User) -> str:
    """Return '1080p' for active subscribers, '720p' for everyone else."""
    return "1080p" if user.subscription_tier_name else "720p"


def get_cache_hit(db, url: str, quality: str):
    """
    Returns CachedVideo if a valid (non-expired) cache entry exists for this
    video_id AND the requested quality r2_key is populated.
    Returns None otherwise.
    """
    video_id = _extract_video_id(url)
    if not video_id:
        return None

    entry = db.query(CachedVideo).filter(CachedVideo.video_id == video_id).first()
    if not entry:
        return None

    if entry.expires_at < datetime.utcnow():
        db.delete(entry)
        db.commit()
        return None

    r2_key = entry.r2_key_1080p if quality == "1080p" else entry.r2_key_720p
    if not r2_key:
        return None

    return entry


def touch_cache(db, entry: CachedVideo):
    """Extend TTL on cache hit."""
    now = datetime.utcnow()
    entry.last_used_at = now
    entry.expires_at = now + timedelta(days=CACHE_TTL_DAYS)
    db.commit()


def store_cache(db, url: str, quality: str, r2_key: str, title: str, duration: float):
    """
    Store or update cache entry after a successful download.
    If entry exists (other quality already cached), just fills the missing key.
    """
    video_id = _extract_video_id(url)
    if not video_id:
        return

    entry = db.query(CachedVideo).filter(CachedVideo.video_id == video_id).first()
    now = datetime.utcnow()

    if entry:
        if quality == "1080p":
            entry.r2_key_1080p = r2_key
        else:
            entry.r2_key_720p = r2_key
        entry.last_used_at = now
        entry.expires_at = now + timedelta(days=CACHE_TTL_DAYS)
        entry.title = title or entry.title
        entry.duration = duration or entry.duration
    else:
        entry = CachedVideo(
            video_id=video_id,
            title=title,
            duration=duration,
            r2_key_720p=r2_key if quality == "720p" else None,
            r2_key_1080p=r2_key if quality == "1080p" else None,
            last_used_at=now,
            expires_at=now + timedelta(days=CACHE_TTL_DAYS),
        )
        db.add(entry)

    db.commit()


def purge_expired(db):
    """Delete all expired cache entries. Call on startup."""
    now = datetime.utcnow()
    expired = db.query(CachedVideo).filter(CachedVideo.expires_at < now).all()
    for e in expired:
        db.delete(e)
    if expired:
        db.commit()
    return len(expired)
