"""Access-log endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_log_service, get_sync_service
from app.schemas import AccessLogRead, LogPullResult, RealtimeEvent, SyncSummary
from app.services.device_client import DeviceError
from app.services.device_service import DeviceNotFoundError
from app.services.log_service import LogService
from app.services.sync_service import SyncService

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("", response_model=list[AccessLogRead], summary="Search stored access logs")
def search_logs(
    device_id: int | None = None,
    employee_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    service: LogService = Depends(get_log_service),
):
    return service.search(
        device_id=device_id,
        employee_id=employee_id,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


@router.get("/count", summary="Total stored log rows")
def count_logs(service: LogService = Depends(get_log_service)):
    return {"count": service.count()}


@router.post("/pull", response_model=SyncSummary, summary="Pull logs from all devices")
def pull_all_logs(
    device_ids: list[int] | None = Query(None),
    since: datetime | None = None,
    until: datetime | None = None,
    service: SyncService = Depends(get_sync_service),
):
    """Fetch the transaction log of every panel.

    `since`/`until` limit what is **stored** (e.g. only today). A panel always
    answers with its whole transaction buffer, so the read itself is not narrowed and
    the rows outside the window are simply not written — they come back as `skipped`.
    """
    return service.pull_all_logs(device_ids, since=since, until=until)


@router.post(
    "/devices/{device_id}/pull", response_model=LogPullResult, summary="Pull logs from one device"
)
def pull_device_logs(
    device_id: int,
    since: datetime | None = None,
    until: datetime | None = None,
    service: LogService = Depends(get_log_service),
):
    try:
        return service.pull_device_logs(device_id, since=since, until=until)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get(
    "/devices/{device_id}/realtime",
    response_model=list[RealtimeEvent],
    summary="Live events (door open/close) from one device",
)
def realtime_events(device_id: int, service: LogService = Depends(get_log_service)):
    try:
        return service.realtime_events(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DeviceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
