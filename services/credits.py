"""Credit accounting + admin logging.

APP_ENV=development bypasses credit checks (localhost / staging).
APP_ENV=production enforces the balance.
"""
import os
import json
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import User, CreditTransaction, AdminLog


def is_dev_mode() -> bool:
    return os.getenv("APP_ENV", "development").lower() in ("development", "dev", "local")


def signup_bonus() -> int:
    return int(os.getenv("SIGNUP_BONUS_CREDITS", 5))


def cost_for_job(max_clips: int) -> int:
    """1 credit per clip, with a minimum of 1."""
    return max(1, int(max_clips))


# ── Mutations (always call inside a request that commits) ────
def _record_txn(
    db: Session,
    user: User,
    kind: str,
    amount: int,
    balance_after: int,
    job_id: Optional[str] = None,
    note: Optional[str] = None,
) -> CreditTransaction:
    txn = CreditTransaction(
        user_id=user.id,
        kind=kind,
        amount=amount,
        balance_after=balance_after,
        job_id=job_id,
        note=note,
    )
    db.add(txn)
    return txn


def deduct(db: Session, user: User, amount: int, job_id: Optional[str] = None, note: Optional[str] = None) -> CreditTransaction:
    """Take credits (subscription first, then topup). Dev mode bypasses checks."""
    if is_dev_mode():
        return _record_txn(db, user, "deduct", -amount, user.credits, job_id, note=f"[DEV bypass] {note or ''}")

    # Import here to avoid circular dependency
    from models import UserSubscription

    # Check total available (subscription + topup)
    subscription = db.query(UserSubscription).filter(
        UserSubscription.user_id == user.id,
        UserSubscription.status.in_(["active", "pending"]),
    ).first()

    sub_credits = subscription.subscription_credits_balance if subscription else 0
    topup_credits = user.topup_credits_balance
    total_available = sub_credits + topup_credits

    if total_available < amount:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient credits: need {amount}, have {total_available} (subscription: {sub_credits}, topup: {topup_credits}). Buy more credits.",
        )

    # Deduct from subscription first, then topup
    if sub_credits >= amount:
        subscription.subscription_credits_balance -= amount
        source = "subscription"
    else:
        need_from_topup = amount - sub_credits
        subscription.subscription_credits_balance = 0
        user.topup_credits_balance -= need_from_topup
        source = "mixed"

    if subscription:
        db.add(subscription)
    db.add(user)
    return _record_txn(db, user, "deduct", -amount, total_available - amount, job_id, note)


def refund(db: Session, user: User, amount: int, job_id: Optional[str] = None, note: Optional[str] = None) -> CreditTransaction:
    """Give back credits previously deducted (e.g. on job failure)."""
    if is_dev_mode():
        return _record_txn(db, user, "refund", amount, user.credits, job_id, note=f"[DEV bypass] {note or ''}")

    user.credits += amount
    return _record_txn(db, user, "refund", amount, user.credits, job_id, note)


def grant(
    db: Session,
    user: User,
    amount: int,
    kind: str = "admin_grant",
    note: Optional[str] = None,
) -> CreditTransaction:
    """Add credits (admin top-up or signup bonus)."""
    user.credits += amount
    return _record_txn(db, user, kind, amount, user.credits, note=note)


# ── Admin audit log ──────────────────────────────────────────
def log_admin(
    db: Session,
    actor: User,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    target_email: Optional[str] = None,
    payload: Optional[dict] = None,
) -> AdminLog:
    entry = AdminLog(
        actor_id=actor.id,
        actor_email=actor.email,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_email=target_email,
        payload=json.dumps(payload) if payload else None,
    )
    db.add(entry)
    return entry
