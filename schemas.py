from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    rank: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    clip_type: str
    score: float
    reason: str
    transcript_excerpt: str
    hook_line: Optional[str] = None
    tags: Optional[str] = None
    status: str
    raw_clip_path: Optional[str] = None
    final_clip_path: Optional[str] = None
    r2_clip_key: Optional[str] = None
    download_url: Optional[str] = None
    user_start_seconds: Optional[float] = None
    user_end_seconds: Optional[float] = None
    user_approved: bool
    user_notes: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    status: str
    stage_progress: int
    source_url: Optional[str] = None
    source_filename: Optional[str] = None
    source_type: str
    detected_language: Optional[str] = None
    detected_topic: Optional[str] = None
    video_duration_seconds: Optional[float] = None
    video_title: Optional[str] = None
    error_message: Optional[str] = None
    clips: List[ClipOut] = []
    created_at: datetime
    completed_at: Optional[datetime] = None


class JobCreate(BaseModel):
    source_url: Optional[str] = None
    max_clips: int = 5
    clip_types: List[str] = ["controversy", "hook_intro", "quotable", "shocking_stat", "myth_bust"]
    min_clip_duration: int = 20
    max_clip_duration: int = 90
    target_aspect_ratio: str = "9:16"


class ClipUpdate(BaseModel):
    user_start_seconds: Optional[float] = None
    user_end_seconds: Optional[float] = None
    user_approved: Optional[bool] = None
    user_notes: Optional[str] = None


class ExportRequest(BaseModel):
    aspect_ratio: str = "9:16"
    caption_style: str = "word_highlight"
    caption_language: Optional[str] = None
    caption_position: str = "bottom"  # top | middle | bottom
    include_captions: bool = True
    output_format: str = "mp4"
    focus_mode: str = "none"  # none | speaker | center | face
    hook_text: Optional[str] = None
    hook_position: str = "top"  # top | center | bottom
    hook_font_scale: float = 1.0
    hook_style: str = "serif_white"
