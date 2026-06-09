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
    topup_credits_balance = Column(Integer, default=0, nullable=False)
    subscription_tier_id = Column(Integer, nullable=True)

    is_active = Column(Boolean, default=True)
    is_email_verified = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)

    jobs = relationship("Job", back_populates="user", cascade="all, delete-orphan")
    credit_txns = relationship("CreditTransaction", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("UserSubscription", back_populates="user", cascade="all, delete-orphan")

    @property
    def subscription_tier_name(self) -> str | None:
        """Returns the display name of the active/canceled-but-valid subscription tier."""
        from datetime import datetime
        active_sub = next(
            (s for s in self.subscriptions if s.status == "active" or (s.status in ("canceled", "paused") and s.current_period_end > datetime.utcnow())),
            None
        )
        return active_sub.tier.display_name if active_sub else None


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

    # R2 key for the source video (original.mp4). Set when upload goes
    # browser→R2 directly, or after URL download finishes and is mirrored.
    r2_source_key = Column(String, nullable=True)

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

    # R2 key for the rendered clip mp4. When set, downloads/streams redirect
    # to R2 (CDN) instead of streaming bytes through the backend.
    r2_clip_key = Column(String, nullable=True)

    user_start_seconds = Column(Float, nullable=True)
    user_end_seconds = Column(Float, nullable=True)
    user_approved = Column(Boolean, default=False)
    user_notes = Column(String, nullable=True)

    rank = Column(Integer)

    error_message = Column(String, nullable=True)

    credit_type = Column(String, default="free", nullable=False)  # "free" | "paid"

    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job", back_populates="clips")


class SubscriptionTier(Base):
    __tablename__ = "subscription_tiers"

    id = Column(Integer, primary_key=True)
    tier_name = Column(String, unique=True, nullable=False)  # "starter" | "pro" | "professional" | "enterprise"
    display_name = Column(String, nullable=False)  # "Starter" | "Pro" | "Professional" | "Enterprise"
    price_paise = Column(Integer, nullable=False)  # ₹999 = 99900 paise

    base_credits = Column(Integer, nullable=False)  # 10, 20, 30, 100
    bonus_percent = Column(Integer, nullable=False)  # 10, 15, 20, 50
    total_credits = Column(Integer, nullable=False)  # calculated: base + (base * bonus / 100)

    max_clips_per_job = Column(Integer, nullable=True)  # null = unlimited
    max_videos_per_month = Column(Integer, nullable=True)
    export_quality = Column(String, nullable=True)  # "sd" | "hd" | "4k"
    features = Column(Text, nullable=True)  # JSON: { "batch_processing": true, ... }

    billing_period = Column(String, default="monthly", nullable=False)  # "monthly" | "annual"
    razorpay_plan_id = Column(String, nullable=True, unique=True)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    subscription_tier_id = Column(Integer, ForeignKey("subscription_tiers.id"), nullable=False)

    razorpay_subscription_id = Column(String, unique=True, nullable=False, index=True)
    razorpay_plan_id = Column(String, nullable=False)

    status = Column(String, default="active", index=True)  # "active" | "paused" | "canceled" | "past_due"
    current_period_start = Column(DateTime, nullable=False)
    current_period_end = Column(DateTime, nullable=False)
    next_billing_date = Column(DateTime, nullable=False)

    subscription_credits_balance = Column(Integer, default=0, nullable=False)
    subscription_credits_used = Column(Integer, default=0, nullable=False)

    is_renewing = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=lambda: datetime.utcnow())
    canceled_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="subscriptions")
    tier = relationship("SubscriptionTier")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    razorpay_order_id = Column(String, unique=True, nullable=False, index=True)
    razorpay_payment_id = Column(String, nullable=True, index=True)
    razorpay_signature = Column(String, nullable=True)

    amount_paise = Column(Integer, nullable=False)  # amount in paise (₹100 = 10000 paise)
    credits_granted = Column(Integer, nullable=False)
    status = Column(String, default="pending", index=True)  # pending, success, failed
    payment_type = Column(String, default="topup", nullable=False)  # "topup" | "subscription_initial"
    subscription_id = Column(String, nullable=True, index=True)  # FK to user_subscriptions.id if tied to subscription

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    verified_at = Column(DateTime, nullable=True)

    user = relationship("User")


class MusicTrack(Base):
    __tablename__ = "music_tracks"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    filename = Column(String, nullable=False)
    duration = Column(Float, nullable=False)
    r2_key = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User")


class CachedVideo(Base):
    __tablename__ = "cached_videos"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    video_id = Column(String, unique=True, nullable=False, index=True)  # YouTube video ID
    title = Column(String, nullable=True)
    duration = Column(Float, nullable=True)
    r2_key_720p = Column(String, nullable=True)   # R2 path for 720p version
    r2_key_1080p = Column(String, nullable=True)  # R2 path for 1080p version
    last_used_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)  # last_used_at + 30 days
    created_at = Column(DateTime, default=datetime.utcnow)
