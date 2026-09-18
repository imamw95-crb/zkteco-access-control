"""Realtime monitoring endpoints.

The stream is newline-delimited JSON, the same shape `POST /api/personnel/sync`
already uses, so the browser reads it with the same fetch + reader loop rather
than needing a second protocol (or a WebSocket dependency).

Unlike the push stream, nothing here can write to a panel — there is no panel
access at all — so there is no pre-flight refusal to make before the response
starts. The trade-off is that the stream reports what the *scheduler* has
stored: with `SCHEDULER_ENABLED=false` it will sit still, which is exactly what
`polling.enabled` in the payload is for.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.deps import get_monitor_service
from app.database import SessionLocal
from app.schemas import MonitorSnapshot
from app.services.monitor_service import (
    DEFAULT_FEED_LIMIT,
    MAX_FEED_LIMIT,
    MonitorService,
)

router = APIRouter(prefix="/monitor", tags=["monitor"])

#: Wall-clock between two database checks. Reading the database is cheap, but the
#: feed only ever moves as fast as the log poller (60s by default), so a much
#: shorter interval buys nothing but requests.
DEFAULT_INTERVAL_SECONDS = 3.0


@router.get(
    "/snapshot",
    response_model=MonitorSnapshot,
    summary="Panel health plus the most recent access events",
)
def monitor_snapshot(
    limit: int = Query(DEFAULT_FEED_LIMIT, ge=1, le=MAX_FEED_LIMIT),
    since: datetime | None = None,
    until: datetime | None = None,
    service: MonitorService = Depends(get_monitor_service),
):
    """`since`/`until` are read as wall-clock dates, so `2026-09-18T00:00:00` means
    "from the start of that day on the panel's own clock", which is what the operator
    typed. See `LogService.wall_clock`."""
    return service.snapshot(limit=limit, since=since, until=until)


@router.get("/stream", summary="Live NDJSON feed: access events and panel health")
def monitor_stream(
    interval: float = Query(
        DEFAULT_INTERVAL_SECONDS,
        ge=1.0,
        le=30.0,
        description="Detik antara dua pemeriksaan database",
    ),
    limit: int = Query(DEFAULT_FEED_LIMIT, ge=1, le=MAX_FEED_LIMIT),
    since: datetime | None = None,
    until: datetime | None = None,
):
    return StreamingResponse(
        _monitor_events(interval=interval, limit=limit, since=since, until=until),
        media_type="application/x-ndjson",
        # No intermediary may buffer this, or "live" turns into "one minute late".
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _monitor_events(
    *,
    interval: float,
    limit: int,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[str]:
    """Yield one JSON object per check, then sleep.

    The iterator runs on a worker thread once the response has started, so it
    opens its **own, short-lived** session per check rather than borrowing the
    request's: a monitoring stream stays open for hours, and a session held that
    long would pin a connection and serve a stale identity map. Nothing is
    written here, so a session per tick costs nothing but the connect.
    """
    cursor: int | None = None
    last_fingerprint: tuple | None = None

    try:
        while True:
            with SessionLocal() as db:
                service = MonitorService(db)
                if cursor is None:
                    events = service.recent_events(limit=limit, since=since, until=until)
                    cursor = service.latest_cursor()
                    kind = "snapshot"
                else:
                    # Always newest first, whichever query produced the rows: the page
                    # prepends each batch, and a newest-first batch stays on top.
                    batch = service.events_after(cursor, limit=limit, since=since, until=until)
                    events = list(reversed(batch))
                    cursor = max([cursor, *(event["id"] for event in events)])
                    kind = "update"
                devices = service.device_statuses()
                fingerprint = MonitorService.fingerprint(devices)
                payload: dict = {
                    "type": kind,
                    "generated_at": datetime.now(timezone.utc),
                    "cursor": cursor,
                    "events": events,
                    "stats": service.stats(since=since, until=until),
                    "polling": service.polling_state(),
                }

            # The device grid is only resent when something about it changed;
            # resending 23 panels every tick would re-render the table under the
            # operator's cursor for no new information.
            if kind == "snapshot" or fingerprint != last_fingerprint:
                payload["devices"] = devices
            last_fingerprint = fingerprint

            # A heartbeat is still a message: it tells the page (and anything
            # between it and here) that the stream is alive on a quiet night.
            yield json.dumps(payload, default=str) + "\n"
            time.sleep(interval)
    except GeneratorExit:  # pragma: no cover - the client went away
        return
