"""Dashboard endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import db_session, get_device_service
from app.schemas import DashboardRow
from app.services.device_service import DeviceService
from app.services.personnel_service import PersonnelService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/devices", response_model=list[DashboardRow], summary="Device toolbar table")
def device_table(service: DeviceService = Depends(get_device_service)):
    """Columns mirroring the ZKAccess toolbar: name, serial, IP, personnel, firmware, status."""
    return service.dashboard_rows()


@router.get("/summary", summary="Fleet counters for the dashboard header")
def summary(service: DeviceService = Depends(get_device_service), db=Depends(db_session)):
    devices = service.list()
    return {
        "devices_total": len(devices),
        "devices_online": sum(1 for d in devices if d.is_online),
        "devices_offline": sum(1 for d in devices if not d.is_online),
        "personnel_total": PersonnelService(db).count(),
        "personnel_on_devices": sum(d.personnel_count or 0 for d in devices),
    }
