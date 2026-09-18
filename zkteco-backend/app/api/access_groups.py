"""Access level (access group) endpoints.

These mirror the ZKAccess "Access Level" screen: a named level, the doors it
opens, and the personnel who hold it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import db_session, get_access_group_service
from app.schemas import (
    AccessGroupChangeResult,
    AccessGroupCreate,
    AccessGroupDetail,
    AccessGroupDoorCreate,
    AccessGroupDoorRead,
    AccessGroupMemberAdd,
    AccessGroupMemberRead,
    AccessGroupRead,
    AccessGroupUpdate,
)
from app.services.access_group_service import (
    AccessGroupNotFoundError,
    AccessGroupService,
    DuplicateAccessGroupError,
)
from app.services.time_zone_service import TimeZoneService

router = APIRouter(prefix="/access-groups", tags=["access-groups"])


def _zone_map(db) -> dict[int, tuple[str, bool]]:
    """device_timezone_id -> (name, is_24_hour), so levels can show their zone."""
    return {
        zone.device_timezone_id: (zone.name, zone.is_24_hour) for zone in TimeZoneService(db).list()
    }


def _read(group, zones: dict[int, tuple[str, bool]]) -> AccessGroupRead:
    name, is_24h = zones.get(group.device_timezone_id, (None, None))
    return AccessGroupRead(
        id=group.id,
        name=group.name,
        description=group.description,
        device_timezone_id=group.device_timezone_id,
        door_count=group.door_count,
        member_count=group.member_count,
        time_zone_name=name,
        is_24_hour=is_24h,
    )


@router.get("", response_model=list[AccessGroupRead], summary="List access levels")
def list_groups(
    service: AccessGroupService = Depends(get_access_group_service),
    db=Depends(db_session),
):
    zones = _zone_map(db)
    return [_read(group, zones) for group in service.list()]


@router.post(
    "",
    response_model=AccessGroupDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create access level",
)
def create_group(
    payload: AccessGroupCreate, service: AccessGroupService = Depends(get_access_group_service)
):
    try:
        return service.create(payload)
    except DuplicateAccessGroupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/{group_id}", response_model=AccessGroupDetail, summary="Access level detail")
def get_group(
    group_id: int,
    service: AccessGroupService = Depends(get_access_group_service),
    db=Depends(db_session),
):
    try:
        group = service.get(group_id)
    except AccessGroupNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    zone_name, is_24h = _zone_map(db).get(group.device_timezone_id, (None, None))
    return AccessGroupDetail(
        id=group.id,
        name=group.name,
        description=group.description,
        device_timezone_id=group.device_timezone_id,
        door_count=group.door_count,
        member_count=group.member_count,
        time_zone_name=zone_name,
        is_24_hour=is_24h,
        doors=[
            AccessGroupDoorRead.model_validate(link)
            for link in sorted(group.doors, key=lambda link: (link.device_id, link.door_number))
        ],
        members=[
            AccessGroupMemberRead(
                personnel_id=link.personnel.id,
                employee_id=link.personnel.employee_id,
                name=link.personnel.name,
                department=link.personnel.department,
            )
            for link in group.member_links
            if link.personnel is not None
        ],
    )


@router.patch("/{group_id}", response_model=AccessGroupRead, summary="Edit access level")
def update_group(
    group_id: int,
    payload: AccessGroupUpdate,
    service: AccessGroupService = Depends(get_access_group_service),
):
    try:
        return service.update(group_id, payload)
    except AccessGroupNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DuplicateAccessGroupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete(
    "/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete access level",
)
def delete_group(group_id: int, service: AccessGroupService = Depends(get_access_group_service)):
    try:
        service.delete(group_id)
    except AccessGroupNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# -- doors -----------------------------------------------------------------
@router.get(
    "/{group_id}/doors", response_model=list[AccessGroupDoorRead], summary="List doors of a level"
)
def list_group_doors(
    group_id: int, service: AccessGroupService = Depends(get_access_group_service)
):
    try:
        group = service.get(group_id)
    except AccessGroupNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return [AccessGroupDoorRead.model_validate(link) for link in group.doors]


@router.post(
    "/{group_id}/doors",
    response_model=AccessGroupDoorRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a door to an access level",
)
def add_group_door(
    group_id: int,
    payload: AccessGroupDoorCreate,
    service: AccessGroupService = Depends(get_access_group_service),
):
    try:
        return service.add_door(group_id, payload)
    except AccessGroupNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete(
    "/{group_id}/doors/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a door from an access level",
)
def remove_group_door(
    group_id: int, link_id: int, service: AccessGroupService = Depends(get_access_group_service)
):
    try:
        service.remove_door(group_id, link_id)
    except AccessGroupNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# -- members ---------------------------------------------------------------
@router.get(
    "/{group_id}/members",
    response_model=list[AccessGroupMemberRead],
    summary="List personnel holding this access level",
)
def list_group_members(
    group_id: int,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    service: AccessGroupService = Depends(get_access_group_service),
):
    try:
        people = service.members(group_id)
    except AccessGroupNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    window = people[offset : offset + limit]
    return [
        AccessGroupMemberRead(
            personnel_id=p.id, employee_id=p.employee_id, name=p.name, department=p.department
        )
        for p in window
    ]


@router.post(
    "/{group_id}/members",
    response_model=AccessGroupChangeResult,
    summary="Add personnel to an access level",
)
def add_group_members(
    group_id: int,
    payload: AccessGroupMemberAdd,
    service: AccessGroupService = Depends(get_access_group_service),
):
    personnel_ids = [payload.personnel_id] if payload.personnel_id else []
    employee_ids = list(payload.employee_ids or [])
    if payload.employee_id:
        employee_ids.append(payload.employee_id)

    if not personnel_ids and not employee_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Sertakan personnel_id, employee_id, atau employee_ids",
        )

    try:
        return service.add_members(group_id, personnel_ids=personnel_ids, employee_ids=employee_ids)
    except AccessGroupNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete(
    "/{group_id}/members/{personnel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a person from an access level",
)
def remove_group_member(
    group_id: int,
    personnel_id: int,
    service: AccessGroupService = Depends(get_access_group_service),
):
    try:
        service.remove_member(group_id, personnel_id)
    except AccessGroupNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
