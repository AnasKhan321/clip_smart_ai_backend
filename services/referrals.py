"""Referral-code helpers and one-time referral payouts."""
import secrets
import string
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Referral, User
from services.credits import grant, referral_bonus


_ALPHABET = string.ascii_uppercase + string.digits


def normalize_referral_code(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    normalized = "".join(ch for ch in code.upper().strip() if ch.isalnum())
    return normalized or None


def ensure_referral_code(db: Session, user: User) -> str:
    """Assign a unique referral code to a user if they do not already have one."""
    existing = normalize_referral_code(user.referral_code)
    if existing:
        if user.referral_code != existing:
            user.referral_code = existing
            db.add(user)
        return existing

    prefix = (user.name or user.email.split("@")[0] or "VIBE").upper()
    prefix = "".join(ch for ch in prefix if ch.isalnum())[:4] or "VIBE"

    for _ in range(20):
        suffix = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        candidate = f"{prefix}{suffix}"
        taken = db.query(User.id).filter(func.upper(User.referral_code) == candidate).first()
        if not taken:
            user.referral_code = candidate
            db.add(user)
            return candidate

    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not generate referral code")


def apply_referral_signup(
    db: Session,
    referred_user: User,
    referral_code: Optional[str],
    referred_credits_awarded: int,
) -> Optional[Referral]:
    """Record a referral and award the referrer once.

    Referral credits intentionally use kind="referral_bonus"; existing expiry
    logic only treats top-up payments, subscriptions, and admin_grant as paid
    preservation signals.
    """
    normalized = normalize_referral_code(referral_code)
    if not normalized:
        return None

    referrer = db.query(User).filter(func.upper(User.referral_code) == normalized).first()
    if not referrer:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid referral code")
    if referrer.id == referred_user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot use your own referral code")

    existing = db.query(Referral).filter(Referral.referred_user_id == referred_user.id).first()
    if existing:
        return existing

    amount = referral_bonus()
    if amount > 0:
        grant(
            db,
            referrer,
            amount,
            kind="referral_bonus",
            note=f"Referral bonus for {referred_user.email}",
        )

    referred_user.referred_by_user_id = referrer.id
    db.add(referred_user)

    referral = Referral(
        referrer_user_id=referrer.id,
        referred_user_id=referred_user.id,
        code=normalized,
        referrer_credits_awarded=amount,
        referred_credits_awarded=referred_credits_awarded,
    )
    db.add(referral)
    return referral
