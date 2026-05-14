"""Admin endpoints. All require User.is_admin == True."""
import os
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from database import get_db
from models import User, CreditTransaction, AdminLog, Job
from auth import require_admin, create_access_token
from services.credits import grant, log_admin, is_dev_mode

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
    total_credits_outstanding: int
    dev_mode: bool


class UnlockIn(BaseModel):
    password: str


class UnlockOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AdminUserOut


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
    return StatsOut(
        total_users=db.query(func.count(User.id)).scalar() or 0,
        total_jobs=db.query(func.count(Job.id)).scalar() or 0,
        total_credits_outstanding=db.query(func.coalesce(func.sum(User.credits), 0)).scalar() or 0,
        dev_mode=is_dev_mode(),
    )


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
    log_admin(
        db, admin_user, "grant_credits",
        target_type="user", target_id=target.id, target_email=target.email,
        payload={"amount": payload.amount, "note": payload.note, "new_balance": target.credits},
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
