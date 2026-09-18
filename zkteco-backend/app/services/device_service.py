"""Device management: CRUD, "Get Information of Device", health and discovery."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Device
from app.schemas import DeviceCreate, DeviceUpdate
from app.services.device_client import (
    DeviceClient,
    DeviceInfo,
    HealthResult,
    build_client,
)

logger = logging.getLogger(__name__)


class DeviceNotFoundError(LookupError):
    pass


class DuplicateDeviceError(ValueError):
    pass


class DeviceService:
    def __init__(self, db: Session):
        self.db = db

    # -- CRUD --------------------------------------------------------------
    def list(self, *, active_only: bool = False) -> list[Device]:
        stmt = select(Device).order_by(Device.name)
        if active_only:
            stmt = stmt.where(Device.is_active.is_(True))
        return list(self.db.scalars(stmt))

    def get(self, device_id: int) -> Device:
        device = self.db.get(Device, device_id)
        if device is None:
            raise DeviceNotFoundError(f"Device {device_id} tidak ditemukan")
        return device

    def get_by_ip(self, ip: str, port: int | None = None) -> Device | None:
        stmt = select(Device).where(Device.ip == ip)
        if port is not None:
            stmt = stmt.where(Device.port == port)
        return self.db.scalars(stmt).first()

    def create(self, payload: DeviceCreate) -> Device:
        device = Device(**payload.model_dump())
        self.db.add(device)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateDeviceError(
                f"Device dengan IP {payload.ip}:{payload.port} sudah terdaftar"
            ) from exc
        self.db.refresh(device)
        return device

    def update(self, device_id: int, payload: DeviceUpdate) -> Device:
        device = self.get(device_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(device, field, value)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateDeviceError("IP/port device bentrok dengan device lain") from exc
        self.db.refresh(device)
        return device

    def delete(self, device_id: int) -> None:
        device = self.get(device_id)
        self.db.delete(device)
        self.db.commit()

    # -- device communication ---------------------------------------------
    def client_for(self, device: Device) -> DeviceClient:
        return build_client(
            device.ip,
            device.port,
            device.password,
            connect_timeout=settings.device_connect_timeout,
            receive_timeout=settings.device_receive_timeout,
            receive_retries=settings.device_receive_retries,
            max_retries=settings.device_max_retries,
            backoff=settings.device_retry_backoff,
        )

    def fetch_info(self, device: Device, *, include_personnel_count: bool = True) -> DeviceInfo:
        """Run "Get Information of Device" against a panel."""
        with self.client_for(device) as client:
            return client.get_info(include_personnel_count=include_personnel_count)

    def refresh_info(self, device_id: int, *, include_personnel_count: bool = True) -> DeviceInfo:
        """Fetch device information and cache it on the Device row."""
        device = self.get(device_id)
        try:
            info = self.fetch_info(device, include_personnel_count=include_personnel_count)
        except Exception as exc:
            self._mark_offline(device, exc)
            raise
        self._apply_info(device, info)
        return info

    def _apply_info(self, device: Device, info: DeviceInfo) -> None:
        device.serial_number = info.serial_number
        device.firmware_version = info.firmware_version
        device.model = info.model
        device.mac_address = info.mac_address
        device.lock_count = info.lock_count
        device.reader_count = info.reader_count
        device.max_user_count = info.max_user_count
        if info.personnel_count is not None:
            device.personnel_count = info.personnel_count
        device.is_online = True
        device.last_seen_at = datetime.now(timezone.utc)
        device.last_error = None
        self.db.commit()
        self.db.refresh(device)

    def _mark_offline(self, device: Device, exc: Exception) -> None:
        device.is_online = False
        device.last_error = str(exc)[:2000]
        self.db.commit()
        logger.warning("device %s (%s) unreachable: %s", device.name, device.ip, exc)

    def check_health(self, device: Device) -> HealthResult:
        client = self.client_for(device)
        result = client.health_check()
        device.is_online = result.online
        if result.online:
            device.last_seen_at = datetime.now(timezone.utc)
            device.last_error = None
        else:
            device.last_error = (result.error or "unknown error")[:2000]
        self.db.commit()
        return result

    def discover(self, interface_address: str | None = None, timeout: int = 3) -> list[dict]:
        """Broadcast-discover panels and flag which ones are already registered."""
        discovered = build_client("0.0.0.0").discover(interface_address, timeout)
        for item in discovered:
            item["registered"] = self.get_by_ip(item["ip"] or "", None) is not None
        return discovered

    # -- dashboard ---------------------------------------------------------
    def dashboard_rows(self) -> list[dict]:
        return [
            {
                "device_name": d.name,
                "serial_number": d.serial_number,
                "ip_address": d.ip,
                "personnel_count": d.personnel_count,
                "firmware_version": d.firmware_version,
                "status": "online" if d.is_online else "offline",
            }
            for d in self.list()
        ]

    def sync_all_health(self, device_ids: list[int] | None = None) -> dict:
        """Health-check every device, independently (see sync_service)."""
        from app.services.sync_service import SyncService

        return SyncService(self.db).health_check_all(device_ids)
