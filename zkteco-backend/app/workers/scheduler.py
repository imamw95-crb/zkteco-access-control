"""Scheduled background jobs (APScheduler).

Jobs are deliberately short and independent: each one fans out over the fleet
and isolates failures per device.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.database import SessionLocal
from app.services.sync_service import SyncService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC")


def _run(job_name: str, fn) -> None:
    session = SessionLocal()
    try:
        result = fn(SyncService(session))
        logger.info(
            "[%s] devices=%s ok=%s failed=%s",
            job_name,
            result["total_devices"],
            result["succeeded"],
            result["failed"],
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("[%s] job crashed: %s", job_name, exc)
    finally:
        session.close()


def poll_logs_job() -> None:
    _run("poll_logs", lambda svc: svc.pull_all_logs())


def health_check_job() -> None:
    _run("health_check", lambda svc: svc.health_check_all())


def refresh_info_job() -> None:
    _run("refresh_info", lambda svc: svc.refresh_all())


def start_scheduler() -> None:
    if not settings.scheduler_enabled or scheduler.running:
        return

    scheduler.add_job(
        poll_logs_job,
        "interval",
        seconds=settings.log_poll_interval_seconds,
        id="poll_logs",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        health_check_job,
        "interval",
        seconds=settings.health_check_interval_seconds,
        id="health_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        refresh_info_job,
        "interval",
        hours=6,
        id="refresh_info",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("scheduler started")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
