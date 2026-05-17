import hashlib
import hmac
import os
import logging
import time
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse
from sqlalchemy.orm import Session
from database import get_db
from models import Clip, Job, User
from schemas import ClipOut, ClipUpdate, ExportRequest
from services.exporter import export_clip
from services.transcriber import load_transcript
from services.editor import render_and_caption_clip
from services import r2
from auth import get_current_user, SECRET_KEY

router = APIRouter()
logger = logging.getLogger(__name__)

# ── Stream-token helpers ────────────────────────────────────────────────────
# <video> / ReactPlayer can't send Authorization headers, so the stream
# endpoint accepts a short-lived HMAC token via query-param instead.

_STREAM_TOKEN_TTL = 4 * 3600  # 4 hours


def _sign_stream_token(clip_id: str) -> str:
    """Create an HMAC token: hex(expires) + '.' + hex(hmac)."""
    expires = int(time.time()) + _STREAM_TOKEN_TTL
    msg = f"{clip_id}:{expires}".encode()
    sig = hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()
    return f"{expires:x}.{sig}"


def _verify_stream_token(clip_id: str, token: str) -> bool:
    """Return True if the token is valid and not expired."""
    try:
        parts = token.split(".", 1)
        if len(parts) != 2:
            return False
        expires = int(parts[0], 16)
        if time.time() > expires:
            return False
        msg = f"{clip_id}:{expires}".encode()
        expected = hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts[1])
    except Exception:
        return False


def serialize_clip(clip: Clip) -> ClipOut:
    """Convert ORM Clip → ClipOut, populating download_url from R2 if available."""
    out = ClipOut.model_validate(clip)
    if clip.r2_clip_key and r2.is_enabled():
        try:
            out.download_url = r2.object_url(clip.r2_clip_key)
        except Exception:
            pass
    return out


def _owned_clip(clip_id: str, db: Session, user: User) -> Clip:
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    job = db.query(Job).filter(Job.id == clip.job_id).first()
    if job and job.user_id and job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your clip")
    return clip


@router.get("/clips/{clip_id}", response_model=ClipOut)
def get_clip(clip_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return serialize_clip(_owned_clip(clip_id, db, user))


@router.patch("/clips/{clip_id}", response_model=ClipOut)
def update_clip(
    clip_id: str,
    body: ClipUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    clip = _owned_clip(clip_id, db, user)

    # Validate trim bounds
    new_start = body.user_start_seconds if body.user_start_seconds is not None \
        else clip.user_start_seconds
    new_end = body.user_end_seconds if body.user_end_seconds is not None \
        else clip.user_end_seconds
    if new_start is not None and new_end is not None and new_end <= new_start:
        raise HTTPException(status_code=400,
                            detail="user_end_seconds must exceed user_start_seconds")

    trim_changed = False
    if body.user_start_seconds is not None:
        clip.user_start_seconds = body.user_start_seconds
        trim_changed = True
    if body.user_end_seconds is not None:
        clip.user_end_seconds = body.user_end_seconds
        trim_changed = True
    if body.user_approved is not None:
        clip.user_approved = body.user_approved
        clip.status = "approved" if body.user_approved else clip.status
    if body.user_notes is not None:
        clip.user_notes = body.user_notes

    db.commit()

    if trim_changed:
        _rerender_clip(clip, db)

    db.refresh(clip)
    return clip


def _rerender_clip(clip: Clip, db: Session):
    clip.status = "rendering"
    clip.error_message = None
    db.commit()

    clip_dict = {
        "rank": clip.rank,
        "start_seconds": clip.start_seconds,
        "end_seconds": clip.end_seconds,
        "user_start_seconds": clip.user_start_seconds,
        "user_end_seconds": clip.user_end_seconds,
    }

    job = db.query(Job).filter(Job.id == clip.job_id).first()
    source_dims = (job.source_width, job.source_height) \
        if (job and job.source_width and job.source_height) else None

    # Pull source from R2 if worker disk was wiped — same reason as exporter.
    try:
        from services.exporter import _ensure_local_source
        _ensure_local_source(clip.job_id)
    except Exception as exc:
        logger.warning("source restore for rerender failed (%s): %s",
                       clip.job_id, exc)

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
    else:
        clip.final_clip_path = result["final_clip_path"]
        clip.raw_clip_path = None
        clip.status = "ready"
        if r2.is_enabled() and result["final_clip_path"]:
            key = r2.clip_key(clip.job_id, clip.rank)
            clip.r2_clip_key = key
            _clip_id = clip.id
            def _clear_key(k, cid=_clip_id):
                from database import SessionLocal
                s = SessionLocal()
                try:
                    c = s.query(Clip).filter(Clip.id == cid).first()
                    if c and c.r2_clip_key == k:
                        c.r2_clip_key = None
                        s.commit()
                finally:
                    s.close()
            r2.upload_in_background(result["final_clip_path"], key,
                                    on_failure=_clear_key)

    db.commit()


@router.post("/clips/{clip_id}/export")
def export_clip_endpoint(
    clip_id: str,
    body: ExportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    clip = _owned_clip(clip_id, db, user)

    clip_dict = {
        "rank": clip.rank,
        "start_seconds": clip.start_seconds,
        "end_seconds": clip.end_seconds,
        "user_start_seconds": clip.user_start_seconds,
        "user_end_seconds": clip.user_end_seconds,
    }

    try:
        export_path = export_clip(
            clip.job_id,
            clip_dict,
            {
                "aspect_ratio": body.aspect_ratio,
                "caption_style": body.caption_style,
                "include_captions": body.include_captions,
                "focus_mode": body.focus_mode,
            },
        )
        clip.status = "exported"
        db.commit()
        return {"export_path": export_path, "download_url": f"/api/clips/{clip_id}/download"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/clips/{clip_id}/stream-url")
def get_stream_url(
    clip_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return a short-lived authenticated stream URL for <video> tags.

    ReactPlayer / <video> can't send Authorization headers, so we mint a
    time-limited HMAC token and embed it in the URL as a query-param.
    """
    clip = _owned_clip(clip_id, db, user)
    token = _sign_stream_token(clip.id)
    return {"stream_url": f"/api/clips/{clip.id}/stream?token={token}"}


@router.get("/clips/{clip_id}/stream")
def stream_clip(
    clip_id: str,
    request: Request,
    token: str = Query(..., description="HMAC stream token from /stream-url"),
    db: Session = Depends(get_db),
):
    """Stream clip video. Requires a valid HMAC token (from /stream-url)."""
    if not _verify_stream_token(clip_id, token):
        raise HTTPException(status_code=401, detail="Invalid or expired stream token")

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    # Prefer R2: zero-egress CDN edge. Skip backend bandwidth entirely.
    if clip.r2_clip_key and r2.is_enabled():
        try:
            return RedirectResponse(url=r2.object_url(clip.r2_clip_key),
                                    status_code=307)
        except Exception:
            pass  # Fall back to local stream

    path = clip.final_clip_path
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Clip file not found")

    file_size = os.path.getsize(path)
    range_header = request.headers.get("range")

    if range_header:
        start, end = 0, file_size - 1
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else file_size - 1
        except Exception:
            pass
        chunk_size = end - start + 1

        def iter_file():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    data = f.read(min(65536, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
            },
        )

    def iter_full():
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        iter_full(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        },
    )


@router.get("/clips/{clip_id}/download")
def download_clip(
    clip_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Resolve a clip download URL.

    Prefers R2: returns JSON `{url: <presigned R2 URL>}` so the browser can
    fetch directly from the CDN edge without proxying through Railway.
    Returning a redirect here was unsafe: the frontend hits this endpoint
    via axios, which auto-follows the 307 and re-sends the `Authorization`
    header to R2 — R2 rejects that (signed-URL header conflict / CORS),
    silently breaking downloads.

    Falls back to streaming the local ephemeral file if R2 isn't usable.
    """
    clip = _owned_clip(clip_id, db, user)

    if clip.r2_clip_key and r2.is_enabled():
        try:
            if r2.object_exists(clip.r2_clip_key):
                return {"url": r2.object_url(clip.r2_clip_key, ttl=7200)}
            else:
                # Self-heal: background upload must have failed permanently.
                # Clear the stale key so future requests skip the R2 check
                # and fall through to the local-file path immediately.
                logger.warning(
                    "r2 object missing for clip %s (key=%s) — clearing stale key",
                    clip_id, clip.r2_clip_key,
                )
                clip.r2_clip_key = None
                db.commit()
        except Exception as exc:
            logger.warning("r2 download URL build failed for clip %s: %s",
                           clip_id, exc)

    from services.editor import get_clips_dir
    export_file = str(get_clips_dir(clip.job_id) / f"clip_{clip.rank:03d}_export.mp4")
    if os.path.exists(export_file):
        path = export_file
    else:
        path = clip.final_clip_path

    if not path or not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail="Clip file not available. R2 upload may still be in progress — "
                   "try again in a moment.",
        )

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"clip_{clip.rank:03d}.mp4",
    )
