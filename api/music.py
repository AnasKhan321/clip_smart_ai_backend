"""Music upload/management endpoints."""
import os
import logging
import subprocess
from uuid import uuid4
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from database import get_db
from models import User, MusicTrack
from auth import get_current_user
from services.media_tools import ffprobe_path
from services import r2

router = APIRouter(prefix="/music", tags=["music"])
logger = logging.getLogger(__name__)

MAX_MUSIC_SIZE_MB = 50
ALLOWED_EXTENSIONS = {".mp3", ".m4a", ".wav"}


class MusicTrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    duration: float
    created_at: str


@router.post("/upload")
def upload_music(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload music file. Returns track metadata."""
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Only {ALLOWED_EXTENSIONS} allowed")

    max_bytes = MAX_MUSIC_SIZE_MB * 1024 * 1024
    if file.size and file.size > max_bytes:
        raise HTTPException(413, f"File exceeds {MAX_MUSIC_SIZE_MB}MB limit")

    track_id = str(uuid4())
    r2_key = f"music/{track_id}{ext}"

    try:
        # Read file content
        content = file.file.read()
        if len(content) > max_bytes:
            raise HTTPException(413, f"File exceeds {MAX_MUSIC_SIZE_MB}MB limit")

        # Get duration via ffprobe
        try:
            duration = _get_audio_duration(content)
        except Exception as e:
            logger.warning("ffprobe failed: %s", e)
            raise HTTPException(400, f"Could not read audio file: {e}")

        # Upload to R2
        if r2.is_enabled():
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(content)
                tmp.flush()
                try:
                    r2.upload_file(tmp.name, r2_key)
                finally:
                    os.unlink(tmp.name)
        else:
            raise HTTPException(503, "R2 storage not configured")

        # Save to DB
        track = MusicTrack(
            id=track_id,
            user_id=user.id,
            filename=file.filename,
            duration=float(duration),
            r2_key=r2_key,
        )
        db.add(track)
        db.commit()
        db.refresh(track)

        return MusicTrackOut.model_validate(track)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Music upload failed")
        raise HTTPException(500, f"Upload failed: {str(e)[:100]}")


def _get_audio_duration(content: bytes) -> float:
    """Get audio duration in seconds via ffprobe."""
    import tempfile
    import json

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(content)
        tmp.flush()
        tmp_path = tmp.name

    try:
        proc = subprocess.run(
            [
                ffprobe_path(),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                tmp_path,
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffprobe error: {proc.stderr.decode()[:200]}")

        data = json.loads(proc.stdout)
        duration = float(data.get("format", {}).get("duration", 0))
        if duration <= 0:
            raise ValueError("Duration is 0 or missing")
        return duration
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@router.get("/my-tracks", response_model=list[MusicTrackOut])
def list_user_tracks(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List user's uploaded music tracks."""
    tracks = (
        db.query(MusicTrack)
        .filter(MusicTrack.user_id == user.id)
        .order_by(MusicTrack.created_at.desc())
        .all()
    )
    return [MusicTrackOut.model_validate(t) for t in tracks]


@router.delete("/tracks/{track_id}")
def delete_music(
    track_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete music track."""
    track = db.query(MusicTrack).filter(
        MusicTrack.id == track_id,
        MusicTrack.user_id == user.id,
    ).first()

    if not track:
        raise HTTPException(404, "Track not found")

    try:
        if r2.is_enabled() and track.r2_key:
            r2.delete_object(track.r2_key)
    except Exception as e:
        logger.warning("R2 delete failed for %s: %s", track.r2_key, e)

    db.delete(track)
    db.commit()

    return {"ok": True}
