from datetime import datetime
from uuid import uuid4
from sqlalchemy import Column, String, Float, Integer, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)

    # Local auth (nullable for OAuth-only accounts)
    password_hash = Column(String, nullable=True)

    # OAuth
    google_id = Column(String, unique=True, index=True, nullable=True)
    auth_provider = Column(String, default="local")  # "local" | "google"

    # Credits + admin
    credits = Column(Integer, default=0, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)

    jobs = relationship("Job", back_populates="user", cascade="all, delete-orphan")
    credit_txns = relationship("CreditTransaction", back_populates="user", cascade="all, delete-orphan")


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)  # "deduct" | "refund" | "admin_grant" | "signup_bonus"
    amount = Column(Integer, nullable=False)  # signed: + for credit, - for debit
    balance_after = Column(Integer, nullable=False)
    job_id = Column(String, nullable=True, index=True)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", back_populates="credit_txns")


class AdminLog(Base):
    __tablename__ = "admin_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    actor_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    actor_email = Column(String, nullable=False)  # denormalized for log readability
    action = Column(String, nullable=False)  # "grant_credits" | "set_admin" | etc.
    target_type = Column(String, nullable=True)  # "user" | "job"
    target_id = Column(String, nullable=True, index=True)
    target_email = Column(String, nullable=True)
    payload = Column(Text, nullable=True)  # JSON-encoded extra info
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)

    source_url = Column(String, nullable=True)
    source_filename = Column(String, nullable=True)
    source_type = Column(String)  # "url" | "upload"

    status = Column(String, default="pending")
    # pending → downloading → transcribing → diarizing → analyzing → clipping → ready | failed

    stage_progress = Column(Integer, default=0)
    error_message = Column(String, nullable=True)

    detected_language = Column(String, nullable=True)
    detected_topic = Column(String, nullable=True)
    video_duration_seconds = Column(Float, nullable=True)
    video_title = Column(String, nullable=True)
    source_width = Column(Integer, nullable=True)
    source_height = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    clips = relationship("Clip", back_populates="job", cascade="all, delete-orphan")
    user = relationship("User", back_populates="jobs")


class Clip(Base):
    __tablename__ = "clips"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    job_id = Column(String, ForeignKey("jobs.id"))

    start_seconds = Column(Float)
    end_seconds = Column(Float)
    duration_seconds = Column(Float)

    clip_type = Column(String)
    # controversy | hook_intro | quotable | myth_bust |
    # emotional_peak | story_arc | shocking_stat | debate_moment

    score = Column(Float)
    reason = Column(String)
    transcript_excerpt = Column(Text)
    hook_line = Column(String, nullable=True)
    tags = Column(String, nullable=True)  # JSON-encoded list

    status = Column(String, default="pending")
    # pending → rendering → ready | approved | discarded | exported

    raw_clip_path = Column(String, nullable=True)
    final_clip_path = Column(String, nullable=True)

    user_start_seconds = Column(Float, nullable=True)
    user_end_seconds = Column(Float, nullable=True)
    user_approved = Column(Boolean, default=False)
    user_notes = Column(String, nullable=True)

    rank = Column(Integer)

    error_message = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job", back_populates="clips")
