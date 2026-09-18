"""Personnel endpoints: central CRUD plus device read-back."""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_personnel_service
from app.database import SessionLocal
from app.schemas import DeviceDataRead, PersonnelCreate, PersonnelRead, PersonnelUpdate
from app.services.access_group_service import AccessGroupNotFoundError
from app.services.department_service import DepartmentNotFoundError
from app.services.device_client import DeviceError
from app.services.device_service import DeviceNotFoundError
from app.services.personnel_service import (
    DuplicatePersonnelError,
    PersonnelNotFoundError,
    PersonnelService,
)
from app.services.push_agent import PushAgentError, PushAgentNotConfigured

router = APIRouter(prefix="/personnel", tags=["personnel"])


@router.get("", response_model=list[PersonnelRead], summary="List personnel")
def list_personnel(
    active_only: bool = False,
    q: str | None = Query(None, description="Search by name or employee id"),
    service: PersonnelService = Depends(get_personnel_service),
):
    return service.list(active_only=active_only, q=q)


@router.post(
    "", response_model=PersonnelRead, status_code=status.HTTP_201_CREATED, summary="Add personnel"
)
def create_personnel(
    payload: PersonnelCreate, service: PersonnelService = Depends(get_personnel_service)
):
    try:
        return service.create(payload)
    except DuplicatePersonnelError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (AccessGroupNotFoundError, DepartmentNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post(
    "/sync",
    summary="Push personnel to every panel their access levels cover",
)
def sync_personnel_everywhere(
    personnel_ids: str = Query(..., description="Id personel, dipisah koma"),
    dry_run: bool = Query(
        False, description="Kembalikan apa yang akan ditulis, tanpa mengirim apa pun"
    ),
    device_ids: str | None = Query(
        None,
        description=(
            "Batasi sapuan ke device ini saja (id dipisah koma). Kosong = semua panel; "
            "perlu semua panel untuk mencabut hak lama"
        ),
    ),
    stream: bool = Query(
        False,
        description=(
            "Kirim hasil tiap panel begitu panel itu selesai (NDJSON), bukan sekaligus di akhir"
        ),
    ),
    service: PersonnelService = Depends(get_personnel_service),
):
    """Write these people into *every* panel their access levels reach.

    A per-device push only refreshes the one panel it is aimed at, so granting
    somebody an access level that spans twenty panels would leave nineteen of them
    stale. Only the named people are sent, so the other users already on each
    panel are never rewritten.

    With `stream=true` the same sweep is reported panel by panel as NDJSON: one
    line per panel, then a summary line. Twenty-three panels take long enough that
    a caller waiting for the final answer cannot tell progress from a hang, and
    cannot say which panels are already written.

    `device_ids` limits the sweep to those panels. Granting a level to somebody who
    was just added to it only concerns that level's panels; the unrestricted
    default is for the sweep that has to hunt down stale rights everywhere.
    """
    try:
        ids = [int(part) for part in personnel_ids.split(",") if part.strip()]
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "personnel_ids harus angka dipisah koma",
        ) from exc
    if not ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "personnel_ids kosong")

    scope: list[int] | None = None
    if device_ids is not None and device_ids.strip():
        try:
            scope = [int(part) for part in device_ids.split(",") if part.strip()]
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "device_ids harus angka dipisah koma",
            ) from exc
        if not scope:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "device_ids kosong")

    if not stream:
        try:
            return service.push_to_all_devices(ids, dry_run=dry_run, device_ids=scope)
        except PushAgentNotConfigured as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    # A stream has already answered 200 by the time the first panel is written, so
    # a missing agent has to be refused here, before anything is sent.
    try:
        service.assert_push_ready(ids, dry_run=dry_run)
    except PushAgentNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return StreamingResponse(_push_events(ids, dry_run, scope), media_type="application/x-ndjson")


def _push_events(
    personnel_ids: list[int], dry_run: bool, device_ids: list[int] | None = None
) -> Iterator[str]:
    """One JSON object per panel, then the summary, as newline-delimited JSON.

    The iterator runs on a worker thread once the response has started, so it
    opens its own database session rather than borrowing the request's — the same
    pattern the fleet sync job uses.
    """
    db = SessionLocal()
    try:
        service = PersonnelService(db)
        events = service.iter_push_to_all_devices(
            personnel_ids, dry_run=dry_run, device_ids=device_ids
        )
        for event in events:
            # `payload` is the whole derived record set for that panel (tens of
            # kilobytes each, megabytes over a sweep) and the caller only needs the
            # counts, so it stays out of the stream.
            compact = {key: value for key, value in event.items() if key != "payload"}
            yield json.dumps(compact, default=str) + "\n"
    finally:
        db.close()


@router.get("/{personnel_id}", response_model=PersonnelRead, summary="Get one person")
def get_personnel(personnel_id: int, service: PersonnelService = Depends(get_personnel_service)):
    try:
        return service.get(personnel_id)
    except PersonnelNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.patch("/{personnel_id}", response_model=PersonnelRead, summary="Edit personnel")
def update_personnel(
    personnel_id: int,
    payload: PersonnelUpdate,
    service: PersonnelService = Depends(get_personnel_service),
):
    try:
        return service.update(personnel_id, payload)
    except PersonnelNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DuplicatePersonnelError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (AccessGroupNotFoundError, DepartmentNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete(
    "/{personnel_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete personnel"
)
def delete_personnel(personnel_id: int, service: PersonnelService = Depends(get_personnel_service)):
    try:
        service.delete(personnel_id)
    except PersonnelNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# -- device side -----------------------------------------------------------
devices_router = APIRouter(prefix="/devices", tags=["personnel"])


@devices_router.get(
    "/{device_id}/personnel",
    response_model=DeviceDataRead,
    summary="Get Personnel Data From Device",
)
def get_personnel_from_device(
    device_id: int,
    service: PersonnelService = Depends(get_personnel_service),
):
    try:
        records = service.pull_from_device(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DeviceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return DeviceDataRead(device_id=device_id, table="user", count=len(records), records=records)


@devices_router.get("/{device_id}/personnel/count", summary="Actual personnel count on device")
def count_personnel_on_device(
    device_id: int, service: PersonnelService = Depends(get_personnel_service)
):
    try:
        return {"device_id": device_id, "personnel_count": service.count_on_device(device_id)}
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DeviceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@devices_router.post("/{device_id}/personnel/import", summary="Import device users into central DB")
def import_personnel_from_device(
    device_id: int,
    overwrite: bool = Query(False),
    service: PersonnelService = Depends(get_personnel_service),
):
    try:
        return service.import_from_device(device_id, overwrite=overwrite)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DeviceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@devices_router.post(
    "/{device_id}/personnel/sync",
    summary="Push personnel + access rights into the device",
)
def sync_personnel_to_device(
    device_id: int,
    dry_run: bool = Query(
        False, description="Kembalikan apa yang akan ditulis, tanpa mengirim apa pun"
    ),
    personnel_ids: str | None = Query(
        None, description="Batasi ke id personel ini saja, dipisah koma"
    ),
    service: PersonnelService = Depends(get_personnel_service),
):
    """Write this panel's users and their access rights into the panel.

    The writing is done by a Windows agent (the official 32-bit Pull SDK DLL
    cannot be loaded here). Use `?dry_run=true` to inspect the records first.
    """
    ids: list[int] | None = None
    if personnel_ids:
        try:
            ids = [int(part) for part in personnel_ids.split(",") if part.strip()]
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "personnel_ids harus angka dipisah koma",
            ) from exc

    try:
        return service.push_to_device(device_id, ids, dry_run=dry_run)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PushAgentNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except PushAgentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
