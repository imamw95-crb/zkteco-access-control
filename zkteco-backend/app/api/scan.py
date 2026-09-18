"""IP-range panel search — separate from the UDP broadcast in ``/devices/discover``.

The broadcast only works inside the backend's own subnet. This endpoint sweeps a CIDR
range with a TCP connect instead, so panels on a routed subnet can be found, and it
reads their serial/firmware to tell a panel apart from any other host with an open
port 4370.

It is read-only: no personnel data, no access rights and no panel network settings are
ever written. Registering a found device (``POST /api/devices``) is a separate,
database-only action.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_scan_service
from app.database import SessionLocal
from app.schemas import NetworkScanRequest, NetworkScanResult
from app.services.network_scan import NetworkScanService, ScanRangeError, parse_hosts

router = APIRouter(prefix="/scan", tags=["scan"])


@router.get("/local-networks", summary="Subnets this host can reach")
def local_networks():
    """The allowed subnets plus the local address used to reach each one.

    ``local_address: null`` means the backend has no route to that subnet, so a scan
    of it will come back empty — which says nothing about the panels themselves.
    """
    return NetworkScanService.local_networks()


@router.post("", response_model=NetworkScanResult, summary="Search an IP range for C3 panels")
def scan_network(
    payload: NetworkScanRequest,
    stream: bool = Query(
        False, description="Report each batch as it finishes (NDJSON) instead of at the end"
    ),
    service: NetworkScanService = Depends(get_scan_service),
):
    """Find panels on ``10.100.1.0/24``, ``192.168.1.0/24``, a single IP, or an ``a-b`` range.

    An open port 4370 is not proof of a panel, so with ``identify=true`` (the default)
    the serial number and firmware are read through ``DeviceClient`` and the result is
    matched against the device table — a panel whose address changed is still found by
    its serial.

    With ``stream=true`` the sweep is reported as newline-delimited JSON: a ``progress``
    line per batch of probed hosts, one ``host`` line per panel found, then a ``summary``
    line. Two /24 ranges take long enough that a caller waiting for the final answer
    cannot tell progress from a hang.
    """
    if not stream:
        try:
            return service.scan(payload)
        except ScanRangeError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # The range is validated BEFORE the stream opens: once 200 has been sent, a bad
    # range can only be reported as a line *inside* the stream.
    try:
        parse_hosts(payload.ranges)
    except ScanRangeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return StreamingResponse(_scan_events(payload), media_type="application/x-ndjson")


def _scan_events(request: NetworkScanRequest) -> Iterator[str]:
    """One JSON object per line.

    The iterator runs on a worker thread once the response has started, so it opens
    its own database session rather than borrowing the request's — the same pattern
    the fleet push and ``SyncService`` use.
    """
    db = SessionLocal()
    try:
        for event in NetworkScanService(db).iter_scan(request):
            yield json.dumps(event, default=str) + "\n"
    finally:
        db.close()
