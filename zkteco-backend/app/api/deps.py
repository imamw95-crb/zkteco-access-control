"""FastAPI dependencies (service wiring + login/role guards)."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ROLE_ADMIN, User
from app.services.access_group_service import AccessGroupService
from app.services.auth_service import AuthService
from app.services.department_service import DepartmentService
from app.services.device_service import DeviceService
from app.services.log_service import LogService
from app.services.monitor_service import MonitorService
from app.services.network_scan import NetworkScanService
from app.services.panel_network import PanelNetworkService
from app.services.personnel_service import PersonnelService
from app.services.sync_service import SyncService
from app.services.time_zone_service import TimeZoneService


def db_session() -> Iterator[Session]:
    yield from get_db()


# ---------------------------------------------------------------------------
# Login and roles
#
# The dashboard hides tabs a role may not use, but that is convenience only:
# every guard below is *server-side*, so a hand-made request is refused too.
# `admin` may do everything; `hr` only manages people data (personnel —
# including the push to panels — departments and access levels). Reading is
# shared: an HR account still needs the device list to pick a door.
# ---------------------------------------------------------------------------
def session_token(request: Request) -> str:
    """The session token from the cookie, or from an `Authorization` header.

    The header form exists so scripts and ops tooling (which have no cookie jar)
    can log in with the same accounts: ``Authorization: Bearer <token>``.
    """
    token = request.cookies.get(settings.auth_cookie_name)
    if token:
        return token
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return ""


def current_user(request: Request, db: Session = Depends(db_session)) -> User:
    """The logged-in user, or **401**. Never returns an anonymous user."""
    user = AuthService(db).resolve(session_token(request))
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Belum login. Silakan masuk dulu.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_login(user: User = Depends(current_user)) -> User:
    """Any active account — the default guard for `/api` routes."""
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    """Admin only: panels, door control, network, jam akses, user accounts."""
    if user.role != ROLE_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Hanya admin yang boleh membuka bagian ini.",
        )
    return user


def get_device_service(db: Session = Depends(db_session)) -> DeviceService:
    return DeviceService(db)


def get_personnel_service(db: Session = Depends(db_session)) -> PersonnelService:
    return PersonnelService(db)


def get_access_group_service(db: Session = Depends(db_session)) -> AccessGroupService:
    return AccessGroupService(db)


def get_time_zone_service(db: Session = Depends(db_session)) -> TimeZoneService:
    return TimeZoneService(db)


def get_department_service(db: Session = Depends(db_session)) -> DepartmentService:
    return DepartmentService(db)


def get_log_service(db: Session = Depends(db_session)) -> LogService:
    return LogService(db)


def get_sync_service(db: Session = Depends(db_session)) -> SyncService:
    return SyncService(db)


def get_scan_service(db: Session = Depends(db_session)) -> NetworkScanService:
    return NetworkScanService(db)


def get_panel_network_service(db: Session = Depends(db_session)) -> PanelNetworkService:
    return PanelNetworkService(db)


def get_monitor_service(db: Session = Depends(db_session)) -> MonitorService:
    return MonitorService(db)
