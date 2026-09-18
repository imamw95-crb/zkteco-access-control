"""FastAPI application entrypoint.

Run locally::

    uvicorn app.main:app --reload

Swagger UI is served at ``/docs`` and ReDoc at ``/redoc``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api import api_router
from app.config import settings
from app.database import Base, engine

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables for a fresh deployment. Alembic remains the source of
    # truth for schema changes; see migrations/.
    Base.metadata.create_all(bind=engine)
    _seed_presets()
    _seed_users()

    from app.workers.scheduler import start_scheduler, stop_scheduler

    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


def _seed_presets() -> None:
    """Make sure the 24-hour access time zone exists.

    Every panel in this deployment runs on time zone slot 1, so the definition
    should be present even on a database created with ``create_all`` rather than
    through the Alembic data migration. Idempotent.
    """
    from app.database import SessionLocal
    from app.services.time_zone_service import TimeZoneService

    session = SessionLocal()
    try:
        created = TimeZoneService(session).seed_presets()
        for zone in created:
            logger.info("seeded access time zone %r (slot %s)", zone.name, zone.device_timezone_id)
    except Exception as exc:  # pragma: no cover - startup must not fail
        session.rollback()
        logger.warning("time zone seeding skipped: %s", exc)
    finally:
        session.close()


def _seed_users() -> None:
    """Create the bootstrap logins, but only when there is no user at all.

    `AUTH_SEED_USERS` (default `admin:admin:admin,hr:hr:hr`) is read here rather
    than written into the migration, so an install can choose its own first
    passwords without editing history. Seeded accounts are flagged
    `must_change_password`: the dashboard keeps warning until they are changed,
    but a login is never blocked — locking the only admin out of the dashboard of
    a LAN box is worse than a default password.
    """
    from app.database import SessionLocal
    from app.services.auth_service import AuthService

    session = SessionLocal()
    try:
        created = AuthService(session).seed_users(settings.auth_seed_users)
        for user in created:
            logger.warning(
                "seeded login %r (role %s) with the password from AUTH_SEED_USERS — "
                "change it in the dashboard (Pengguna → Ganti password)",
                user.username,
                user.role,
            )
    except Exception as exc:  # pragma: no cover - startup must not fail
        session.rollback()
        logger.warning("user seeding skipped: %s", exc)
    finally:
        session.close()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Custom backend replacing ZKAccess 3.5 for ZKTeco C3-100/200/300/400 "
        "panels, with no door or user limits."
    ),
    lifespan=lifespan,
)

app.include_router(api_router)


@app.get("/health", tags=["meta"], summary="Service health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": app.version}


_DASHBOARD_HTML = (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse, tags=["meta"], summary="Mini dashboard")
def dashboard() -> str:
    return _DASHBOARD_HTML
