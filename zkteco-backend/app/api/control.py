"""Manual device control and door schedules."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import db_session, get_device_service
from app.models import DoorSchedule
from app.schemas import ControlResult, DoorScheduleCreate, DoorScheduleRead, OpenDoorRequest
from app.services.device_client import DeviceError
from app.services.device_service import DeviceNotFoundError, DeviceService

router = APIRouter(prefix="/control", tags=["control"])
schedules_router = APIRouter(prefix="/door-schedules", tags=["control"])


def _run(device_id: int, action: str, fn) -> ControlResult:
    try:
        fn()
        return ControlResult(device_id=device_id, action=action, success=True)
    except DeviceError as exc:
        return ControlResult(device_id=device_id, action=action, success=False, error=str(exc))


@router.post("/devices/{device_id}/open", response_model=ControlResult, summary="Open a door")
def open_door(
    device_id: int,
    payload: OpenDoorRequest,
    service: DeviceService = Depends(get_device_service),
):
    try:
        device = service.get(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    client = service.client_for(device)
    return _run(
        device_id,
        "open_door",
        lambda: (
            client.connect(),
            client.open_door(payload.door_number, payload.duration_seconds),
            client.disconnect(),
        ),
    )


@router.post(
    "/devices/{device_id}/cancel-alarm", response_model=ControlResult, summary="Cancel alarms"
)
def cancel_alarm(device_id: int, service: DeviceService = Depends(get_device_service)):
    try:
        device = service.get(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    client = service.client_for(device)
    return _run(
        device_id,
        "cancel_alarm",
        lambda: (client.connect(), client.cancel_alarm(), client.disconnect()),
    )


@router.post("/devices/{device_id}/restart", response_model=ControlResult, summary="Restart panel")
def restart_device(device_id: int, service: DeviceService = Depends(get_device_service)):
    try:
        device = service.get(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    client = service.client_for(device)
    return _run(
        device_id,
        "restart",
        lambda: (client.connect(), client.restart(), client.disconnect()),
    )


@router.post(
    "/devices/{device_id}/normal-open", response_model=ControlResult, summary="Toggle normal open"
)
def set_normal_open(
    device_id: int,
    door_number: int = 1,
    enable: bool = True,
    service: DeviceService = Depends(get_device_service),
):
    try:
        device = service.get(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    client = service.client_for(device)
    return _run(
        device_id,
        "normal_open" if enable else "normal_close",
        lambda: (
            client.connect(),
            client.set_normal_open(door_number, enable),
            client.disconnect(),
        ),
    )


@router.get("/devices/{device_id}/door-status", summary="Read door status")
def door_status(
    device_id: int,
    door_number: int = 1,
    service: DeviceService = Depends(get_device_service),
):
    try:
        device = service.get(device_id)
        client = service.client_for(device)
        client.connect()
        try:
            value = client.door_status(door_number)
        finally:
            client.disconnect()
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DeviceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return {"device_id": device_id, "door_number": door_number, "status": value}


# -- schedules -------------------------------------------------------------
@schedules_router.get("", response_model=list[DoorScheduleRead], summary="List door schedules")
def list_schedules(device_id: int | None = None, db: Session = Depends(db_session)):
    stmt = select(DoorSchedule).order_by(DoorSchedule.device_id, DoorSchedule.door_number)
    if device_id is not None:
        stmt = stmt.where(DoorSchedule.device_id == device_id)
    return list(db.scalars(stmt))


@schedules_router.post(
    "",
    response_model=DoorScheduleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create/update door schedule",
)
def create_schedule(payload: DoorScheduleCreate, db: Session = Depends(db_session)):
    existing = db.scalars(
        select(DoorSchedule).where(
            DoorSchedule.device_id == payload.device_id,
            DoorSchedule.door_number == payload.door_number,
            DoorSchedule.day_of_week == payload.day_of_week,
        )
    ).first()

    if existing:
        for field, value in payload.model_dump().items():
            setattr(existing, field, value)
        db.commit()
        db.refresh(existing)
        return existing

    schedule = DoorSchedule(**payload.model_dump())
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@schedules_router.delete(
    "/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete door schedule"
)
def delete_schedule(schedule_id: int, db: Session = Depends(db_session)):
    schedule = db.get(DoorSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule tidak ditemukan")
    db.delete(schedule)
    db.commit()
