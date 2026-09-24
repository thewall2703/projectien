from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from sqlalchemy.orm import Session

from backend.auth import (
    get_current_user,
    set_session_cookie,
    clear_session_cookie,
    verify_password,
)
from backend.config import settings
from backend.database import get_db
from backend.models import User
from backend.schemas import AuthConfigOut, GoogleLoginRequest, LoginRequest, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _admin_email_set() -> set[str]:
    return {part.strip().lower() for part in settings.admin_emails.split(",") if part.strip()}


@router.get("/config", response_model=AuthConfigOut)
def auth_config() -> AuthConfigOut:
    return AuthConfigOut(
        google_client_id=settings.google_client_id,
        google_allowed_domain=settings.google_allowed_domain,
    )


@router.post("/login", response_model=UserOut)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> User:
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    set_session_cookie(response, user.id)
    return user


@router.post("/google", response_model=UserOut)
def google_login(
    payload: GoogleLoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> User:
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    try:
        idinfo = id_token.verify_oauth2_token(
            payload.credential,
            google_requests.Request(),
            settings.google_client_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Google credential") from exc

    email = str(idinfo.get("email") or "").lower().strip()
    domain = settings.google_allowed_domain.lower().strip()
    hd = str(idinfo.get("hd") or "").lower().strip()
    if (
        not email
        or not idinfo.get("email_verified")
        or hd != domain
        or not email.endswith(f"@{domain}")
    ):
        raise HTTPException(status_code=403, detail="Google account not allowed")

    user = db.query(User).filter(User.email == email).first()
    make_admin = email in _admin_email_set()
    if user is None:
        user = User(email=email, password_hash="", is_admin=make_admin)
        db.add(user)
    elif make_admin and not user.is_admin:
        user.is_admin = True
    db.commit()
    db.refresh(user)
    set_session_cookie(response, user.id)
    return user


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user
