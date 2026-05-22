"""Auth endpoints: /signup, /signin, /google, /me, /logout."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import User
from auth import (
    hash_password,
    verify_password,
    create_access_token,
    verify_google_id_token,
    verify_google_access_token,
    get_current_user,
    is_admin_email,
    create_verification_token,
    decode_verification_token,
)
from services.credits import grant, signup_bonus
from services.email import send_verification_email

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Schemas ──────────────────────────────────────────────────
class SignUpIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: Optional[str] = None


class SignInIn(BaseModel):
    email: EmailStr
    password: str


class GoogleSignInIn(BaseModel):
    id_token: Optional[str] = None
    access_token: Optional[str] = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: Optional[str]
    avatar_url: Optional[str]
    auth_provider: str
    credits: int
    is_admin: bool
    is_email_verified: bool
    created_at: datetime


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ── Endpoints ────────────────────────────────────────────────
@router.post("/signup", response_model=AuthOut)
def signup(payload: SignUpIn, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    is_admin = is_admin_email(payload.email)
    user = User(
        email=payload.email,
        name=payload.name or payload.email.split("@")[0],
        password_hash=hash_password(payload.password),
        auth_provider="local",
        is_admin=is_admin,
        is_email_verified=True,  # Set to True to temporarily bypass verification requirements
        last_login_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()

    bonus = signup_bonus()
    if bonus > 0:
        grant(db, user, bonus, kind="signup_bonus", note="Welcome bonus")

    db.commit()
    db.refresh(user)

    # Verification temporarily bypassed
    # if not user.is_email_verified:
    #     token = create_verification_token(user.email, user.id)
    #     background_tasks.add_task(send_verification_email, user.email, user.name, token)

    return AuthOut(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@router.post("/signin", response_model=AuthOut)
def signin(payload: SignInIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.password_hash:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This account uses Google sign-in. Please use the Google button instead.")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")

    if is_admin_email(user.email) and not user.is_admin:
        user.is_admin = True
    user.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    return AuthOut(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@router.post("/google", response_model=AuthOut)
def google_signin(payload: GoogleSignInIn, db: Session = Depends(get_db)):
    if not payload.id_token and not payload.access_token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide id_token or access_token")
    if payload.id_token:
        info = verify_google_id_token(payload.id_token)
    else:
        info = verify_google_access_token(payload.access_token)
    if not info["email_verified"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Google email not verified")

    user = (
        db.query(User)
        .filter(
            (User.google_id == info["google_id"])
            | (func.lower(User.email) == info["email"].lower())
        )
        .first()
    )

    if user and not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")

    new_user = False
    if user:
        if not user.google_id:
            user.google_id = info["google_id"]
            user.auth_provider = "google" if not user.password_hash else user.auth_provider
        if info.get("avatar_url"):
            user.avatar_url = info["avatar_url"]
        if info.get("name") and not user.name:
            user.name = info["name"]
        if is_admin_email(user.email) and not user.is_admin:
            user.is_admin = True
        user.is_email_verified = True
    else:
        user = User(
            email=info["email"],
            name=info.get("name"),
            avatar_url=info.get("avatar_url"),
            google_id=info["google_id"],
            auth_provider="google",
            is_admin=is_admin_email(info["email"]),
            is_email_verified=True,
        )
        db.add(user)
        db.flush()
        new_user = True

    if new_user:
        bonus = signup_bonus()
        if bonus > 0:
            grant(db, user, bonus, kind="signup_bonus", note="Welcome bonus (Google)")

    user.last_login_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Google account already linked to another user")
    db.refresh(user)

    return AuthOut(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@router.post("/dev-login", response_model=AuthOut)
def dev_login(db: Session = Depends(get_db)):
    """Auto-login for local development. Disabled in production."""
    import os
    if os.getenv("APP_ENV", "development").lower() not in ("development", "dev", "local"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    DEV_EMAIL = "dev@localhost"
    user = db.query(User).filter(User.email == DEV_EMAIL).first()
    if not user:
        user = User(
            email=DEV_EMAIL,
            name="Dev User",
            auth_provider="dev",
            is_admin=True,
            is_email_verified=True,
            last_login_at=datetime.utcnow(),
        )
        db.add(user)
        db.flush()
        bonus = signup_bonus()
        if bonus > 0:
            grant(db, user, bonus, kind="signup_bonus", note="Dev account")
        db.commit()
        db.refresh(user)
    else:
        user.is_email_verified = True
        user.last_login_at = datetime.utcnow()
        db.commit()
        db.refresh(user)

    return AuthOut(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)


@router.post("/verify-email", response_model=UserOut)
def verify_email(token: str, db: Session = Depends(get_db)):
    payload = decode_verification_token(token)
    if not payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired verification token")
    user_id = payload.get("sub")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user.is_email_verified = True
    db.commit()
    db.refresh(user)
    return user


@router.post("/resend-verification")
def resend_verification(background_tasks: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.is_email_verified:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already verified")
    if user.auth_provider != "local":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only local auth accounts require email verification")
    token = create_verification_token(user.email, user.id)
    background_tasks.add_task(send_verification_email, user.email, user.name, token)
    return {"message": "Verification email sent"}
