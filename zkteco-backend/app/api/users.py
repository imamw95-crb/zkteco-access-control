"""User accounts — admin only.

An account's role *is* the permission model (see `app/services/auth_service.py`):
`admin` opens every tab, `hr` only the people side. The whole router is therefore
guarded, with no read-only view of the account list for HR.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import db_session, require_admin
from app.models import User
from app.schemas import UserCreate, UserRead, UserUpdate
from app.services.auth_service import (
    AuthService,
    DuplicateUsername,
    InvalidUsername,
    LastAdmin,
    UserNotFound,
    WeakPassword,
)

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_admin)])

#: Service error → HTTP status. 409 for "this would break the data or lock you
#: out", 400 for "that value is not acceptable".
_STATUS = {
    DuplicateUsername: status.HTTP_409_CONFLICT,
    LastAdmin: status.HTTP_409_CONFLICT,
    UserNotFound: status.HTTP_404_NOT_FOUND,
    InvalidUsername: status.HTTP_400_BAD_REQUEST,
    WeakPassword: status.HTTP_400_BAD_REQUEST,
}


def _http(exc: Exception) -> HTTPException:
    return HTTPException(_STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST), str(exc))


@router.get("", response_model=list[UserRead], summary="Daftar pengguna")
def list_users(db: Session = Depends(db_session)):
    return AuthService(db).list_users()


@router.post(
    "", response_model=UserRead, status_code=status.HTTP_201_CREATED, summary="Tambah pengguna"
)
def create_user(payload: UserCreate, db: Session = Depends(db_session)):
    try:
        return AuthService(db).create_user(
            username=payload.username,
            password=payload.password,
            role=payload.role,
            full_name=payload.full_name,
        )
    except (DuplicateUsername, InvalidUsername, WeakPassword) as exc:
        raise _http(exc) from exc


@router.patch("/{user_id}", response_model=UserRead, summary="Ubah pengguna")
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(db_session)):
    """Change a role, deactivate an account, or reset a password.

    Deactivating or resetting also kills that account's live sessions, so the
    change takes effect immediately instead of when the cookie happens to expire.
    The last active admin cannot be demoted or switched off — that would leave
    nobody able to administer the system.
    """
    try:
        return AuthService(db).update_user(
            user_id,
            role=payload.role,
            full_name=payload.full_name,
            is_active=payload.is_active,
            password=payload.password,
            must_change_password=payload.must_change_password,
        )
    except (DuplicateUsername, InvalidUsername, LastAdmin, UserNotFound, WeakPassword) as exc:
        raise _http(exc) from exc


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Hapus pengguna")
def delete_user(
    user_id: int, actor: User = Depends(require_admin), db: Session = Depends(db_session)
):
    try:
        AuthService(db).delete_user(user_id, actor_id=actor.id)
    except (LastAdmin, UserNotFound) as exc:
        raise _http(exc) from exc
