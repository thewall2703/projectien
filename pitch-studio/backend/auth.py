from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
serializer = URLSafeTimedSerializer(settings.secret_key)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


def set_session_cookie(response: Response, user_id: int) -> None:
    token = serializer.dumps({"user_id": user_id})
    response.set_cookie(
        settings.cookie_name,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.cookie_max_age,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.cookie_name, path="/")


def user_id_from_cookie(token: str | None) -> int | None:
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=settings.cookie_max_age)
    except (BadSignature, SignatureExpired):
        return None
    raw = data.get("user_id")
    return int(raw) if raw is not None else None


def get_current_user(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.cookie_name),
) -> User:
    user_id = user_id_from_cookie(session_token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    try:
        user = db.get(User, user_id)
    except OperationalError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is temporarily unreachable",
        ) from exc
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def bootstrap_admin(db: Session) -> None:
    """Ensure the configured bootstrap admin exists with the current password."""
    email = settings.admin_email.lower().strip()
    if not email or not settings.admin_password:
        return
    user = db.query(User).filter(User.email == email).first()
    password_hash = hash_password(settings.admin_password)
    if user is None:
        db.add(User(email=email, password_hash=password_hash, is_admin=True))
    else:
        user.password_hash = password_hash
        user.is_admin = True
    db.commit()
