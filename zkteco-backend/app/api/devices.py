"""Device management endpoints."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_device_service, get_panel_network_service, require_admin
from app.schemas import (
    DeviceCreate,
    DeviceHealth,
    DeviceInfoRead,
    DeviceRead,
    DeviceUpdate,
    DiscoveredDevice,
    PanelNetworkChangeRead,
    PanelNetworkRead,
    PanelNetworkUpdate,
    SyncSummary,
)
from app.services.device_client import DeviceError
from app.services.device_service import (
    DeviceNotFoundError,
    DeviceService,
    DuplicateDeviceError,
)
from app.services.panel_network import (
    NetworkChangeError,
    PanelNetworkChange,
    PanelNetworkService,
    PanelNetworkWriteDisabled,
)
from app.services.push_agent import PushAgentError

router = APIRouter(prefix="/devices", tags=["devices"])

#: Reading the fleet is open to every logged-in role (HR needs the device list to
#: pick a door), but *changing* it is not: creating, editing or deleting a panel,
#: asking it for info, finding new ones on the LAN and rewriting its address are
#: all admin-only. See `app/api/__init__.py`.
admin_only = [Depends(require_admin)]


@router.get("", response_model=list[DeviceRead], summary="List devices")
def list_devices(
    active_only: bool = False,
    service: DeviceService = Depends(get_device_service),
):
    return service.list(active_only=active_only)


@router.post(
    "",
    response_model=DeviceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add device",
    dependencies=admin_only,
)
def create_device(payload: DeviceCreate, service: DeviceService = Depends(get_device_service)):
    try:
        return service.create(payload)
    except DuplicateDeviceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get(
    "/discover",
    response_model=list[DiscoveredDevice],
    summary="Search devices on LAN",
    dependencies=admin_only,
)
def discover_devices(
    interface: str | None = Query(None, description="Local interface address, e.g. 10.100.1.10"),
    timeout: int = Query(3, ge=1, le=30),
    service: DeviceService = Depends(get_device_service),
):
    return service.discover(interface, timeout)


@router.get("/{device_id}", response_model=DeviceRead, summary="Get one device")
def get_device(device_id: int, service: DeviceService = Depends(get_device_service)):
    try:
        return service.get(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.patch(
    "/{device_id}", response_model=DeviceRead, summary="Edit device", dependencies=admin_only
)
def update_device(
    device_id: int, payload: DeviceUpdate, service: DeviceService = Depends(get_device_service)
):
    try:
        return service.update(device_id, payload)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DuplicateDeviceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete device",
    dependencies=admin_only,
)
def delete_device(device_id: int, service: DeviceService = Depends(get_device_service)):
    try:
        service.delete(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post(
    "/{device_id}/info",
    response_model=DeviceInfoRead,
    summary="Get Information of Device",
    dependencies=admin_only,
)
def fetch_device_info(
    device_id: int,
    personnel_count: bool = Query(True, description="Also count users stored on the panel"),
    service: DeviceService = Depends(get_device_service),
):
    try:
        info = service.refresh_info(device_id, include_personnel_count=personnel_count)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DeviceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return DeviceInfoRead(
        device_id=device_id,
        ip=service.get(device_id).ip,
        reachable=True,
        serial_number=info.serial_number,
        firmware_version=info.firmware_version,
        device_model=info.model,
        mac_address=info.mac_address,
        lock_count=info.lock_count,
        reader_count=info.reader_count,
        max_user_count=info.max_user_count,
        max_fingerprint_count=info.max_fingerprint_count,
        personnel_count=info.personnel_count,
        door_status=info.door_status,
        raw_parameters={k: str(v) for k, v in info.raw.items()},
    )


@router.post(
    "/{device_id}/health",
    response_model=DeviceHealth,
    summary="Check online status",
    dependencies=admin_only,
)
def check_device_health(device_id: int, service: DeviceService = Depends(get_device_service)):
    try:
        device = service.get(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    result = service.check_health(device)
    return DeviceHealth(
        device_id=device.id,
        name=device.name,
        ip=device.ip,
        is_online=result.online,
        latency_ms=result.latency_ms,
        error=result.error,
    )


@router.post(
    "/sync/refresh",
    response_model=SyncSummary,
    summary="Refresh all devices",
    dependencies=admin_only,
)
def refresh_all_devices(
    service: DeviceService = Depends(get_device_service),
    device_ids: list[int] | None = Query(None),
):
    return service.sync_all_health(device_ids)


# ---------------------------------------------------------------------------
# The panel's OWN network configuration — ZKAccess' "Modify IP Address".
#
# Reading is always allowed. Writing is switched off unless
# PANEL_NETWORK_WRITE_ENABLED is set, and `dry_run` defaults to TRUE: a wrong
# address leaves the panel unreachable and can only be corrected on site, so the
# write has to be asked for explicitly twice (dry run, then dry_run=false).
# ---------------------------------------------------------------------------
def _network_change(change: PanelNetworkChange) -> PanelNetworkChangeRead:
    return PanelNetworkChangeRead(
        device_id=change.device_id,
        name=change.name,
        current=PanelNetworkRead(**asdict(change.current)),
        new_ip=change.new_ip,
        netmask=change.netmask,
        gateway=change.gateway,
        enabled=change.enabled,
        dry_run=change.dry_run,
        written=change.written,
        reachable_at_new_address=change.reachable_at_new_address,
        record_updated=change.record_updated,
        warnings=change.warnings,
        params=change.params,
    )


@router.get(
    "/{device_id}/network",
    response_model=PanelNetworkRead,
    summary="Read the panel's own network configuration",
)
def read_device_network(
    device_id: int, service: PanelNetworkService = Depends(get_panel_network_service)
):
    """What the panel says its own `IPAddress`/`NetMask`/`GATEWAY`/`MAC` are (read-only)."""
    try:
        return PanelNetworkRead(**asdict(service.read(device_id)))
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DeviceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post(
    "/{device_id}/network",
    response_model=PanelNetworkChangeRead,
    summary="Change the panel's own IP address (opt-in, dry run by default)",
    dependencies=admin_only,
)
def change_device_network(
    device_id: int,
    payload: PanelNetworkUpdate,
    dry_run: bool = Query(
        True,
        description=(
            "Hitung dan validasi saja, tanpa mengirim apa pun. Nilai harus dicek "
            "operator dulu; `dry_run=false` yang benar-benar menulis ke panel."
        ),
    ),
    service: PanelNetworkService = Depends(get_panel_network_service),
):
    """Send a panel to a new address instead of walking to it.

    This is the only operation here that can make a panel **unreachable with no way
    back**: a wrong address has to be corrected at the device itself. So it is refused
    outright unless `PANEL_NETWORK_WRITE_ENABLED` is set, and the connection dies the
    moment the write lands — verification therefore dials the *new* address, and the
    stored address is only updated when that succeeds.
    """
    try:
        change = service.apply(device_id, payload, dry_run=dry_run)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PanelNetworkWriteDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except NetworkChangeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except (DeviceError, PushAgentError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return _network_change(change)
