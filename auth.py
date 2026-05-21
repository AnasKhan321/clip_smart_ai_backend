"""JWT + password hashing + Google ID token verification."""
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from database import get_db
from models import User

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "change-me-in-prod-please")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = int(os.getenv("AUTH_TOKEN_DAYS", 30))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/signin", auto_error=False)


# ── Password hashing ─────────────────────────────────────────
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT ──────────────────────────────────────────────────────
def create_access_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


# ── Google ID token verify ───────────────────────────────────
def verify_google_id_token(token: str) -> dict:
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(500, "GOOGLE_CLIENT_ID not configured on server")
    try:
        info = google_id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except ValueError as e:
        logger.error("Google ID token verification failed: %s", e)
        raise HTTPException(401, f"Invalid Google token: {e}")

    email = info.get("email")
    if not email:
        logger.error("Google ID token missing email claim")
        raise HTTPException(401, "Google token: email claim missing")

    ev = info.get("email_verified", True)
    if isinstance(ev, str):
        ev = ev.lower() == "true"

    return {
        "google_id": info["sub"],
        "email": email,
        "name": info.get("name"),
        "avatar_url": info.get("picture"),
        "email_verified": ev,
    }


# ── Google access token verify (useGoogleLogin flow) ────────
def verify_google_access_token(access_token: str) -> dict:
    import httpx
    try:
        resp = httpx.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error("Google userinfo returned %s: %s", resp.status_code, resp.text[:300])
            raise HTTPException(401, f"Google token rejected (status {resp.status_code})")
        info = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Google userinfo request failed: %s", e)
        raise HTTPException(401, f"Could not verify Google token: {e}")

    if not info.get("sub"):
        logger.error("Google userinfo missing sub: %s", list(info.keys()))
        raise HTTPException(401, "Google token: missing user ID")

    email = info.get("email")
    if not email:
        logger.error("Google userinfo missing email (scopes may be missing): %s", list(info.keys()))
        raise HTTPException(401, "Google token: email not granted — please re-authorise and allow email access")

    # email_verified may be bool (v3) or string "true"/"false" (v1)
    ev = info.get("email_verified", True)
    if isinstance(ev, str):
        ev = ev.lower() == "true"

    return {
        "google_id": info["sub"],
        "email": email,
        "name": info.get("name"),
        "avatar_url": info.get("picture"),
        "email_verified": ev,
    }


# ── FastAPI dependencies ─────────────────────────────────────
def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


def get_current_user_optional(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if not token:
        return None
    user_id = decode_token(token)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


# ── Admin seeding ───────────────────────────────────────────
def admin_emails() -> set[str]:
    raw = os.getenv("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_admin_email(email: str) -> bool:
    return email.lower() in admin_emails()
