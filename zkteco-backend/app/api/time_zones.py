"""Access-control time zone ("jam akses") endpoints.

Mirrors the ZKAccess "Time Zone" screen: named weekly schedules that panels
store in their `timezone` table and access levels reference by number.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import db_session, get_time_zone_service, require_admin
from app.models import AccessGroup, AccessTimeZone
from app.schemas import (
    SetGroupTimeZone,
    TimeZoneCreate,
    TimeZoneDayRead,
    TimeZoneDetail,
    TimeZoneRead,
    TimeZoneUpdate,
)
from app.services.time_zone_service import (
    DAYS,
    DuplicateTimeZoneError,
    TimeZoneInUseError,
    TimeZoneNotFoundError,
    TimeZoneService,
)

router = APIRouter(prefix="/time-zones", tags=["time-zones"])

#: Reading a jam akses is open to HR — creating an access level has to pick one.
#: *Changing* one is admin-only: a wrong schedule can lock people out of doors they
#: can reach today, so it belongs to the same category as the panel settings.
admin_only = [Depends(require_admin)]


def _week_view(zone: AccessTimeZone) -> list[TimeZoneDayRead]:
    week = []
    for day in range(7):
        segments = [f"{slot.start_time}-{slot.end_time}" for slot in zone.active_slots(day)]
        week.append(TimeZoneDayRead(day_of_week=day, day_name=DAYS[day], segments=segments))
    return week


def _detail(service: TimeZoneService, zone: AccessTimeZone, db) -> TimeZoneDetail:
    used = int(
        db.scalar(
            select(func.count())
            .select_from(AccessGroup)
            .where(AccessGroup.device_timezone_id == zone.device_timezone_id)
        )
        or 0
    )
    return TimeZoneDetail(
        id=zone.id,
        name=zone.name,
        device_timezone_id=zone.device_timezone_id,
        description=zone.description,
        is_24_hour=zone.is_24_hour,
        slots=list(zone.slots),
        week=_week_view(zone),
        access_group_count=used,
    )


@router.get("", response_model=list[TimeZoneRead], summary="List access time zones")
def list_time_zones(service: TimeZoneService = Depends(get_time_zone_service)):
    return service.list()


@router.post(
    "",
    response_model=TimeZoneDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a time zone",
    dependencies=admin_only,
)
def create_time_zone(
    payload: TimeZoneCreate, service: TimeZoneService = Depends(get_time_zone_service)
):
    try:
        zone = service.create(payload)
    except DuplicateTimeZoneError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _detail(service, zone, service.db)


@router.post(
    "/presets/24-hours",
    response_model=TimeZoneDetail,
    summary="Create the 24-hour time zone if it is missing",
    dependencies=admin_only,
)
def create_24_hour_preset(service: TimeZoneService = Depends(get_time_zone_service)):
    zone = service.ensure_24_hour(1)
    return _detail(service, zone, service.db)


@router.get("/{time_zone_id}", response_model=TimeZoneDetail, summary="Time zone detail")
def get_time_zone(time_zone_id: int, service: TimeZoneService = Depends(get_time_zone_service)):
    try:
        zone = service.get(time_zone_id)
    except TimeZoneNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _detail(service, zone, service.db)


@router.patch(
    "/{time_zone_id}",
    response_model=TimeZoneDetail,
    summary="Edit a time zone",
    dependencies=admin_only,
)
def update_time_zone(
    time_zone_id: int,
    payload: TimeZoneUpdate,
    service: TimeZoneService = Depends(get_time_zone_service),
):
    try:
        zone = service.update(time_zone_id, payload)
    except TimeZoneNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DuplicateTimeZoneError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _detail(service, zone, service.db)


@router.delete(
    "/{time_zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a time zone",
    dependencies=admin_only,
)
def delete_time_zone(time_zone_id: int, service: TimeZoneService = Depends(get_time_zone_service)):
    try:
        service.delete(time_zone_id)
    except TimeZoneNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except TimeZoneInUseError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


# -- linking an access level ------------------------------------------------
groups_router = APIRouter(prefix="/access-groups", tags=["time-zones"])


@groups_router.put(
    "/{group_id}/time-zone",
    response_model=TimeZoneDetail,
    summary="Set the access time zone of an access level",
)
def set_group_time_zone(
    group_id: int,
    payload: SetGroupTimeZone,
    service: TimeZoneService = Depends(get_time_zone_service),
):
    try:
        service.set_group_time_zone(group_id, payload.time_zone_id)
        zone = service.get(payload.time_zone_id)
    except TimeZoneNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _detail(service, zone, service.db)


@groups_router.get(
    "/{group_id}/time-zone",
    response_model=TimeZoneDetail | None,
    summary="Get the access time zone of an access level",
)
def get_group_time_zone(
    group_id: int,
    service: TimeZoneService = Depends(get_time_zone_service),
    db=Depends(db_session),
):
    group = db.get(AccessGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Access level {group_id} tidak ditemukan")

    zone = service.for_group(group)
    if zone is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Access level ini memakai time zone nomor {group.device_timezone_id} "
            "yang belum didefinisikan",
        )
    return _detail(service, zone, db)
