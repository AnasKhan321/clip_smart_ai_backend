"""Admin endpoints. All require User.is_admin == True."""
import csv
import io
import os
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from database import get_db
from models import User, CreditTransaction, AdminLog, Job, Clip, SubscriptionTier, UserSubscription
from auth import require_admin, create_access_token
from services.credits import grant, log_admin, is_dev_mode
from services.app_settings import load_app_settings, save_app_settings

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Schemas ──────────────────────────────────────────────────
class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: Optional[str]
    avatar_url: Optional[str]
    auth_provider: str
    credits: int
    is_admin: bool
    is_active: bool
    created_at: datetime
    last_login_at: Optional[datetime]
    job_count: int


class GrantCreditsIn(BaseModel):
    email: EmailStr
    amount: int = Field(gt=0, le=100000, description="Credits to add (positive)")
    note: Optional[str] = Field(None, max_length=500)


class GrantCreditsOut(BaseModel):
    user_email: str
    granted: int
    new_balance: int


class CreditTxnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    user_email: Optional[str]
    kind: str
    amount: int
    balance_after: int
    job_id: Optional[str]
    note: Optional[str]
    created_at: datetime


class AdminLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_email: str
    action: str
    target_type: Optional[str]
    target_id: Optional[str]
    target_email: Optional[str]
    payload: Optional[str]
    created_at: datetime


class StatsOut(BaseModel):
    total_users: int
    total_jobs: int
    active_jobs: int
    failed_jobs: int
    total_clips: int
    total_exports: int
    total_credits_outstanding: int
    total_credits_spent: int
    dev_mode: bool


class UnlockIn(BaseModel):
    password: str


class UnlockOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AdminUserOut


class AppSettingsOut(BaseModel):
    maintenance_mode: bool


class AppSettingsIn(BaseModel):
    maintenance_mode: bool


# ── Endpoints ────────────────────────────────────────────────

_ADMIN_SYSTEM_EMAIL = "admin@system"


@router.post("/unlock", response_model=UnlockOut)
def admin_unlock(payload: UnlockIn, db: Session = Depends(get_db)):
    """Password-based admin login. No prior auth required."""
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not admin_password:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ADMIN_PASSWORD not configured")
    if payload.password != admin_password:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin password")

    user = db.query(User).filter(User.email == _ADMIN_SYSTEM_EMAIL).first()
    if not user:
        user = User(
            email=_ADMIN_SYSTEM_EMAIL,
            name="Admin",
            auth_provider="password",
            is_admin=True,
            is_active=True,
            credits=0,
        )
        db.add(user)
        db.flush()
        db.commit()
        db.refresh(user)

    user.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    job_count = db.query(func.count(Job.id)).filter(Job.user_id == user.id).scalar() or 0
    user_out = AdminUserOut(
        id=user.id, email=user.email, name=user.name, avatar_url=user.avatar_url,
        auth_provider=user.auth_provider, credits=user.credits,
        is_admin=user.is_admin, is_active=user.is_active,
        created_at=user.created_at, last_login_at=user.last_login_at,
        job_count=job_count,
    )
    return UnlockOut(access_token=create_access_token(user.id), user=user_out)
@router.get("/stats", response_model=StatsOut)
def stats(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    active_statuses = ["pending", "downloading", "transcribing", "diarizing", "analyzing", "clipping", "exporting"]
    total_credits_spent = db.query(func.coalesce(func.sum(func.abs(CreditTransaction.amount)), 0)).filter(
        CreditTransaction.amount < 0
    ).scalar() or 0
    return StatsOut(
        total_users=db.query(func.count(User.id)).scalar() or 0,
        total_jobs=db.query(func.count(Job.id)).scalar() or 0,
        active_jobs=db.query(func.count(Job.id)).filter(Job.status.in_(active_statuses)).scalar() or 0,
        failed_jobs=db.query(func.count(Job.id)).filter(Job.status == "failed").scalar() or 0,
        total_clips=db.query(func.count(Clip.id)).scalar() or 0,
        total_exports=db.query(func.count(Clip.id)).filter(Clip.status == "ready").scalar() or 0,
        total_credits_outstanding=db.query(func.coalesce(func.sum(User.credits), 0)).scalar() or 0,
        total_credits_spent=total_credits_spent,
        dev_mode=is_dev_mode(),
    )


@router.get("/settings", response_model=AppSettingsOut)
def get_settings(_admin: User = Depends(require_admin)):
    settings = load_app_settings()
    return AppSettingsOut(maintenance_mode=bool(settings.get("maintenance_mode")))


@router.patch("/settings", response_model=AppSettingsOut)
def update_settings(
    payload: AppSettingsIn,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    before = load_app_settings()
    updated = save_app_settings({"maintenance_mode": payload.maintenance_mode})
    log_admin(
        db, admin_user, "set_maintenance_mode",
        target_type="app", target_id="settings",
        payload={
            "before": bool(before.get("maintenance_mode")),
            "after": bool(updated.get("maintenance_mode")),
        },
    )
    db.commit()
    return AppSettingsOut(maintenance_mode=bool(updated.get("maintenance_mode")))


@router.get("/users", response_model=List[AdminUserOut])
def list_users(
    q: Optional[str] = Query(None, description="Search email or name"),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    query = db.query(User)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(or_(func.lower(User.email).like(like), func.lower(User.name).like(like)))

    users = query.order_by(User.created_at.desc()).limit(limit).all()

    job_counts = dict(
        db.query(Job.user_id, func.count(Job.id))
        .filter(Job.user_id.isnot(None))
        .group_by(Job.user_id)
        .all()
    )

    return [
        AdminUserOut(
            id=u.id, email=u.email, name=u.name, avatar_url=u.avatar_url,
            auth_provider=u.auth_provider, credits=u.credits,
            is_admin=u.is_admin, is_active=u.is_active,
            created_at=u.created_at, last_login_at=u.last_login_at,
            job_count=job_counts.get(u.id, 0),
        )
        for u in users
    ]


@router.post("/credits/grant", response_model=GrantCreditsOut)
def grant_credits(
    payload: GrantCreditsIn,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    target = db.query(User).filter(func.lower(User.email) == payload.email.lower()).first()
    if not target:
        raise HTTPException(404, f"No user with email {payload.email}")

    grant(db, target, payload.amount, kind="admin_grant", note=payload.note or f"Granted by {admin_user.email}")

    # Convert all free clips to paid (admin granted credits, so preserve clips)
    from models import Clip, Job
    free_clips = db.query(Clip).join(Job).filter(
        Job.user_id == target.id,
        Clip.credit_type == "free"
    ).all()
    for clip in free_clips:
        clip.credit_type = "paid"

    log_admin(
        db, admin_user, "grant_credits",
        target_type="user", target_id=target.id, target_email=target.email,
        payload={"amount": payload.amount, "note": payload.note, "new_balance": target.credits, "clips_preserved": len(free_clips)},
    )
    db.commit()
    db.refresh(target)

    return GrantCreditsOut(user_email=target.email, granted=payload.amount, new_balance=target.credits)


@router.get("/credits/transactions", response_model=List[CreditTxnOut])
def list_transactions(
    user_email: Optional[str] = None,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    query = db.query(CreditTransaction, User.email).join(User, CreditTransaction.user_id == User.id)
    if user_email:
        query = query.filter(func.lower(User.email) == user_email.lower())
    rows = query.order_by(CreditTransaction.created_at.desc()).limit(limit).all()

    return [
        CreditTxnOut(
            id=t.id, user_id=t.user_id, user_email=email,
            kind=t.kind, amount=t.amount, balance_after=t.balance_after,
            job_id=t.job_id, note=t.note, created_at=t.created_at,
        )
        for t, email in rows
    ]


@router.get("/logs", response_model=List[AdminLogOut])
def list_logs(
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    logs = db.query(AdminLog).order_by(AdminLog.created_at.desc()).limit(limit).all()
    return [AdminLogOut.model_validate(l) for l in logs]


class PreserveFreeClipsIn(BaseModel):
    user_email: EmailStr


class PreserveFreeClipsOut(BaseModel):
    user_email: str
    clips_converted: int


@router.post("/preserve-clips", response_model=PreserveFreeClipsOut)
def preserve_free_clips(
    payload: PreserveFreeClipsIn,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """Convert user's free clips to paid, exempting them from expiry deletion."""
    target = db.query(User).filter(func.lower(User.email) == payload.user_email.lower()).first()
    if not target:
        raise HTTPException(404, f"No user with email {payload.user_email}")

    free_clips = db.query(Clip).join(Job, Clip.job_id == Job.id).filter(
        Job.user_id == target.id,
        Clip.credit_type == "free",
    ).all()

    for clip in free_clips:
        clip.credit_type = "paid"

    log_admin(
        db, admin_user, "preserve_clips",
        target_type="user", target_id=target.id, target_email=target.email,
        payload={"clips_converted": len(free_clips)},
    )

    db.commit()
    return PreserveFreeClipsOut(user_email=target.email, clips_converted=len(free_clips))


class CleanupOut(BaseModel):
    task_id: Optional[str] = None
    mode: str  # "async" (queued to worker) | "sync" (ran inline)
    result: Optional[dict] = None


@router.post("/cleanup-expired", response_model=CleanupOut)
def trigger_cleanup_expired(
    sync: bool = Query(False, description="Run inline and return counts instead of queuing"),
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """Manually run the expired-free-clip sweep (same task beat runs at 02:00 UTC).

    Default queues to the Celery worker (tests the deployed worker path). Pass
    ?sync=true to run inline in the API process and get the deleted/purged counts
    back immediately — handy for clearing the existing backlog on demand.
    """
    from tasks.cleanup import cleanup_expired_free_clips

    log_admin(db, admin_user, "trigger_cleanup_expired",
              target_type="task", target_id="cleanup_expired_free_clips",
              payload={"sync": sync})
    db.commit()

    if sync:
        result = cleanup_expired_free_clips.run()
        return CleanupOut(mode="sync", result=result)

    async_result = cleanup_expired_free_clips.delay()
    return CleanupOut(mode="async", task_id=async_result.id)


def _csv_response(filename: str, rows: list[dict], fieldnames: list[str]) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/users.csv")
def export_users_csv(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    job_counts = dict(
        db.query(Job.user_id, func.count(Job.id))
        .filter(Job.user_id.isnot(None))
        .group_by(Job.user_id)
        .all()
    )
    rows = [
        {
            "id": u.id,
            "email": u.email,
            "name": u.name or "",
            "auth_provider": u.auth_provider,
            "credits": u.credits,
            "jobs": job_counts.get(u.id, 0),
            "is_admin": u.is_admin,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else "",
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else "",
        }
        for u in users
    ]
    fields = ["id", "email", "name", "auth_provider", "credits", "jobs", "is_admin", "is_active", "created_at", "last_login_at"]
    return _csv_response("users.csv", rows, fields)


@router.get("/export/jobs.csv")
def export_jobs_csv(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    jobs = db.query(Job, User.email).join(User, Job.user_id == User.id, isouter=True).order_by(Job.created_at.desc()).all()
    rows = [
        {
            "id": j.id,
            "user_email": email or "",
            "status": j.status,
            "source_type": getattr(j, "source_type", ""),
            "source_url": getattr(j, "source_url", "") or "",
            "video_duration_seconds": getattr(j, "video_duration_seconds", "") or "",
            "created_at": j.created_at.isoformat() if j.created_at else "",
            "completed_at": j.completed_at.isoformat() if getattr(j, "completed_at", None) else "",
        }
        for j, email in jobs
    ]
    fields = ["id", "user_email", "status", "source_type", "source_url", "video_duration_seconds", "created_at", "completed_at"]
    return _csv_response("jobs.csv", rows, fields)


@router.get("/export/transactions.csv")
def export_transactions_csv(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    rows_raw = (
        db.query(CreditTransaction, User.email)
        .join(User, CreditTransaction.user_id == User.id)
        .order_by(CreditTransaction.created_at.desc())
        .all()
    )
    rows = [
        {
            "id": t.id,
            "user_email": email,
            "kind": t.kind,
            "amount": t.amount,
            "balance_after": t.balance_after,
            "job_id": t.job_id or "",
            "note": t.note or "",
            "created_at": t.created_at.isoformat() if t.created_at else "",
        }
        for t, email in rows_raw
    ]
    fields = ["id", "user_email", "kind", "amount", "balance_after", "job_id", "note", "created_at"]
    return _csv_response("transactions.csv", rows, fields)


# ── Subscription Management ──────────────────────────────────
class CreateSubscriptionTierIn(BaseModel):
    tier_name: str = Field(min_length=3, max_length=50)
    display_name: str = Field(min_length=3, max_length=50)
    price_paise: int = Field(gt=0)
    base_credits: int = Field(gt=0)
    bonus_percent: int = Field(ge=0, le=100)
    billing_period: str = "monthly"


class SubscriptionTierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tier_name: str
    display_name: str
    price_paise: int
    base_credits: int
    bonus_percent: int
    total_credits: int
    is_active: bool


class UserSubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    subscription_tier_id: int
    status: str
    current_period_start: datetime
    current_period_end: datetime
    next_billing_date: datetime
    subscription_credits_balance: int
    created_at: datetime


class ManualGrantIn(BaseModel):
    tier_id: int = Field(gt=0)
    days: int = Field(gt=0, le=365, description="Number of days to grant")


@router.post("/tiers", response_model=SubscriptionTierOut)
def create_subscription_tier(
    req: CreateSubscriptionTierIn,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> SubscriptionTierOut:
    """Create new subscription tier (admin only)."""
    from services.subscriptions import calculate_credits

    # Check if tier already exists
    existing = db.query(SubscriptionTier).filter(
        SubscriptionTier.tier_name == req.tier_name
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tier '{req.tier_name}' already exists"
        )

    total_credits = calculate_credits(req.base_credits, req.bonus_percent)

    tier = SubscriptionTier(
        tier_name=req.tier_name,
        display_name=req.display_name,
        price_paise=req.price_paise,
        base_credits=req.base_credits,
        bonus_percent=req.bonus_percent,
        total_credits=total_credits,
        billing_period=req.billing_period,
        is_active=True,
    )

    db.add(tier)
    db.commit()
    db.refresh(tier)

    return SubscriptionTierOut.model_validate(tier)


@router.get("/subscriptions", response_model=list[UserSubscriptionOut])
def get_user_subscriptions(
    user_email: str = Query(...),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[UserSubscriptionOut]:
    """Get all subscriptions for a user (admin only)."""
    user = db.query(User).filter(User.email == user_email).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_email} not found"
        )

    subscriptions = db.query(UserSubscription).filter(
        UserSubscription.user_id == user.id
    ).all()

    return [UserSubscriptionOut.model_validate(s) for s in subscriptions]


@router.post("/subscriptions/{user_id}/manual-grant", response_model=UserSubscriptionOut)
def grant_subscription_manually(
    user_id: str,
    req: ManualGrantIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UserSubscriptionOut:
    """Manually grant subscription to user for N days (admin only)."""
    from datetime import timedelta as td

    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )

    tier = db.query(SubscriptionTier).filter(
        SubscriptionTier.id == req.tier_id
    ).first()

    if not tier:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tier {req.tier_id} not found"
        )

    # Check if user already has active subscription
    existing = db.query(UserSubscription).filter(
        UserSubscription.user_id == user_id,
        UserSubscription.status == "active",
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already has active subscription"
        )

    # Create manual subscription (no Razorpay ID, use placeholder)
    now = datetime.utcnow()
    period_end = now + td(days=req.days)

    subscription = UserSubscription(
        user_id=user_id,
        subscription_tier_id=tier.id,
        razorpay_subscription_id=f"manual_{user_id}_{datetime.utcnow().timestamp()}",
        razorpay_plan_id="manual",
        status="active",
        current_period_start=now,
        current_period_end=period_end,
        next_billing_date=period_end,
        subscription_credits_balance=tier.total_credits,
        subscription_credits_used=0,
        is_renewing=False,  # Don't auto-renew manual grants
    )

    # Grant credits
    txn = CreditTransaction(
        user_id=user_id,
        kind="subscription_grant",
        amount=tier.total_credits,
        balance_after=user.credits + tier.total_credits,
        note=f"Manual grant by {admin.email}: {tier.display_name} ({req.days} days)",
    )

    db.add(subscription)
    db.add(txn)

    log_admin(
        db,
        admin,
        "grant_subscription",
        target_type="user",
        target_id=user_id,
        target_email=user.email,
        payload={
            "tier_id": req.tier_id,
            "tier_name": tier.display_name,
            "days": req.days,
            "credits": tier.total_credits,
        },
    )

    db.commit()
    db.refresh(subscription)

    return UserSubscriptionOut.model_validate(subscription)
