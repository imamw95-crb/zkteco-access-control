"""Fleet-wide operations.

Every operation is executed **per device, independently**: one unreachable
panel must never abort the job for the other 22. Work is spread over a bounded
thread pool because the Pull SDK is blocking-socket based.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Device, SyncRun
from app.services.device_service import DeviceService
from app.services.log_service import LogService

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, db: Session):
        self.db = db

    # -- helpers -----------------------------------------------------------
    def _devices(self, device_ids: list[int] | None) -> list[Device]:
        stmt = select(Device).where(Device.is_active.is_(True)).order_by(Device.name)
        if device_ids:
            stmt = stmt.where(Device.id.in_(device_ids))
        return list(self.db.scalars(stmt))

    def _run_per_device(self, job: str, device: Device, worker) -> dict:
        """Run ``worker`` for one device inside its own DB session."""
        session = SessionLocal()
        run = SyncRun(job=job, device_id=device.id)
        session.add(run)
        session.commit()

        try:
            detail = worker(session, device.id)
            run.success = True
            run.items_processed = int(detail.get("items", 0))
            detail.update({"device_id": device.id, "name": device.name, "ok": True})
        except Exception as exc:
            session.rollback()
            run.success = False
            run.error = str(exc)[:2000]
            logger.warning("[%s] device %s (%s) failed: %s", job, device.name, device.ip, exc)
            detail = {
                "device_id": device.id,
                "name": device.name,
                "ip": device.ip,
                "ok": False,
                "error": str(exc),
            }
        finally:
            run.finished_at = datetime.now(timezone.utc)
            session.add(run)
            session.commit()
            session.close()

        return detail

    def _run_fleet(self, job: str, device_ids: list[int] | None, worker) -> dict:
        devices = self._devices(device_ids)
        details: list[dict] = []

        if not devices:
            return {"job": job, "total_devices": 0, "succeeded": 0, "failed": 0, "details": []}

        with ThreadPoolExecutor(max_workers=settings.device_parallelism) as pool:
            futures = {
                pool.submit(self._run_per_device, job, device, worker): device for device in devices
            }
            for future in as_completed(futures):
                device = futures[future]
                try:
                    details.append(future.result())
                except Exception as exc:  # pragma: no cover - defensive
                    details.append(
                        {
                            "device_id": device.id,
                            "name": device.name,
                            "ok": False,
                            "error": str(exc),
                        }
                    )

        details.sort(key=lambda d: d["device_id"])
        return {
            "job": job,
            "total_devices": len(devices),
            "succeeded": sum(1 for d in details if d.get("ok")),
            "failed": sum(1 for d in details if not d.get("ok")),
            "details": details,
        }

    # -- jobs --------------------------------------------------------------
    def health_check_all(self, device_ids: list[int] | None = None) -> dict:
        def worker(session: Session, device_id: int) -> dict:
            result = DeviceService(session).check_health(DeviceService(session).get(device_id))
            if not result.online:
                raise RuntimeError(result.error or "offline")
            return {"ip": result.latency_ms, "items": 1}

        return self._run_fleet("health_check", device_ids, worker)

    def refresh_all(self, device_ids: list[int] | None = None) -> dict:
        """Refresh serial/firmware/personnel-count cache for the whole fleet."""

        def worker(session: Session, device_id: int) -> dict:
            service = DeviceService(session)
            info = service.refresh_info(device_id)
            return {
                "serial_number": info.serial_number,
                "personnel_count": info.personnel_count,
                "items": info.personnel_count or 0,
            }

        return self._run_fleet("refresh_info", device_ids, worker)

    def pull_all_logs(
        self,
        device_ids: list[int] | None = None,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict:
        """Poll the transaction table of every panel and store new events.

        ``since``/``until`` only narrow what is stored (see
        :meth:`LogService.pull_device_logs`); the scheduled poller passes neither, so
        the incremental job keeps the whole history in sync.
        """

        def worker(session: Session, device_id: int) -> dict:
            result = LogService(session).pull_device_logs(device_id, since=since, until=until)
            if result.get("error"):
                raise RuntimeError(result["error"])
            return {
                "fetched": result["fetched"],
                "inserted": result["inserted"],
                "skipped": result["skipped"],
                "items": result["inserted"],
            }

        return self._run_fleet("pull_logs", device_ids, worker)
