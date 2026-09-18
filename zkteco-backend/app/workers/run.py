"""Standalone scheduler process.

The compose `worker` service runs this instead of the API so that polling keeps
running independent of HTTP traffic::

    python -m app.workers.run
"""

from __future__ import annotations

import logging
import signal
import time

from app.config import settings
from app.database import Base, engine
from app.workers.scheduler import scheduler, start_scheduler, stop_scheduler

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("worker")

_stopping = False


def _handle_signal(signum, _frame) -> None:
    global _stopping
    logger.info("received signal %s, shutting down", signum)
    _stopping = True


def main() -> None:
    Base.metadata.create_all(bind=engine)

    start_scheduler()
    if not scheduler.running:  # pragma: no cover
        logger.error("scheduler did not start (SCHEDULER_ENABLED=%s)", settings.scheduler_enabled)
        return

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "worker up; log poll every %ss, health check every %ss",
        settings.log_poll_interval_seconds,
        settings.health_check_interval_seconds,
    )
    while not _stopping:
        time.sleep(1)

    stop_scheduler()


if __name__ == "__main__":
    main()
