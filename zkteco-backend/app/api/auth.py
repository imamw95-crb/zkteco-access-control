"""Login, logout and "who am I" endpoints.

These are the only `/api` routes that may be called without a session, which is
why they are the only ones registered without a guard in `app/api/__init__.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import current_user, db_session, session_token
from app.config import settings
from app.models import User
from app.schemas import LoginRequest, PasswordChange, UserRead
from app.services.auth_service import (
    AuthService,
    InactiveUser,
    InvalidCredentials,
    WeakPassword,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_session_hours * 3600,
        # Not readable by JavaScript, so a script injected into the page cannot
        # steal the session; `lax` still allows a normal link into the dashboard.
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.auth_cookie_name, path="/")


@router.post("/login", response_model=UserRead, summary="Masuk ke dashboard")
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(db_session),
):
    """Exchange a username and password for a session cookie.

    There is deliberately no failed-attempt lockout: this is a LAN host with a
    handful of operator accounts, and a lockout would mostly be a way to shut the
    only admin out of the dashboard. Every failure answers with the same message
    (and takes the same time) so the form cannot be used to guess usernames.
    """
    service = AuthService(db)
    try:
        user = service.authenticate(payload.username, payload.password)
    except InactiveUser as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except InvalidCredentials as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    set_session_cookie(response, service.open_session(user))
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Keluar")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(db_session),
) -> None:
    """End the session. Always succeeds, even with an expired or bogus cookie.

    Un-guarded on purpose: logging out must work precisely when the session is
    already broken, otherwise the cookie would stay in the browser forever.
    """
    AuthService(db).close_session(session_token(request))
    clear_session_cookie(response)


@router.get("/me", response_model=UserRead, summary="Siapa yang sedang login")
def me(user: User = Depends(current_user)):
    return user


@router.post("/password", response_model=UserRead, summary="Ganti password sendiri")
def change_password(
    payload: PasswordChange,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    """Change your own password; every *other* session of this account is revoked."""
    service = AuthService(db)
    try:
        updated = service.change_password(user, payload.current_password, payload.new_password)
    except (InvalidCredentials, WeakPassword) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    # Anyone else holding a session of this account is logged out — that is the
    # point of changing a password. This browser keeps its own session, so the
    # operator is not thrown out of what they were doing.
    service.close_other_sessions(user.id, session_token(request))
    return updated
