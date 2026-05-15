import os
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from database import get_db
from models import Clip, Job, User
from schemas import ClipOut, ClipUpdate, ExportRequest
from services.exporter import export_clip
from services.transcriber import load_transcript
from services.editor import render_clip, burn_captions
from auth import get_current_user

router = APIRouter()


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
    return _owned_clip(clip_id, db, user)


@router.patch("/clips/{clip_id}", response_model=ClipOut)
def update_clip(
    clip_id: str,
    body: ClipUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    clip = _owned_clip(clip_id, db, user)

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

    # Re-render if trim changed
    if trim_changed:
        _rerender_clip(clip, db)

    db.refresh(clip)
    return clip


def _rerender_clip(clip: Clip, db: Session):
    clip.status = "rendering"
    db.commit()

    clip_dict = {
        "rank": clip.rank,
        "start_seconds": clip.start_seconds,
        "end_seconds": clip.end_seconds,
        "user_start_seconds": clip.user_start_seconds,
        "user_end_seconds": clip.user_end_seconds,
    }

    try:
        result = render_clip(clip.job_id, clip_dict)
        transcript = load_transcript(clip.job_id)
        captioned = result["final_clip_path"].replace("_final.mp4", "_captioned.mp4")
        burn_captions(clip_dict, transcript, result["final_clip_path"], captioned)
        clip.raw_clip_path = result["raw_clip_path"]
        clip.final_clip_path = captioned
        clip.status = "ready"
    except Exception as e:
        clip.status = "ready"

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


@router.get("/clips/{clip_id}/stream")
def stream_clip(clip_id: str, request: Request, db: Session = Depends(get_db)):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

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
    clip = _owned_clip(clip_id, db, user)

    from services.editor import get_clips_dir
    export_file = str(get_clips_dir(clip.job_id) / f"clip_{clip.rank:03d}_export.mp4")
    if os.path.exists(export_file):
        path = export_file
    else:
        path = clip.final_clip_path

    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Clip file not found")

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"clip_{clip.rank:03d}.mp4",
    )
