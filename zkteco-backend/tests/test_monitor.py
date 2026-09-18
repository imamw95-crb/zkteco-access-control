"""Tests for the Monitoring tab: panel health and the live access-event feed.

Monitoring must stay a **database-only** view. A panel accepts a single
connection at a time (`AGENTS.md` rule 12), so a monitor that dialled panels
would fight the scheduler and could make healthy panels look offline — the first
test locks that in.

The stream endpoint itself is covered through its generator plus the OpenAPI
surface rather than an HTTP call: the stream is deliberately endless, and a test
that opens it would have to be torn down mid-iteration.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone

from app.api.monitor import _monitor_events
from app.models import AccessLog
from app.services.monitor_service import MonitorService

BASE_TIME = datetime(2026, 9, 18, 7, 0, tzinfo=timezone.utc)
_keys = itertools.count(1)


def add_log(db, device, *, pin=None, card=None, code=0, door=1, seconds=0):
    """Insert one stored access event, the way the log poller would."""
    row = AccessLog(
        device_id=device.id,
        device_serial=device.serial_number,
        event_time=BASE_TIME + timedelta(seconds=seconds),
        event_code=code,
        event_type=f"EVENT_{code}",
        card_number=card,
        employee_id=pin,
        door_number=door,
        dedupe_key=f"monitor-{next(_keys)}",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# It must not touch the panels
# ---------------------------------------------------------------------------
def test_monitoring_reads_the_database_and_never_dials_a_panel(db, make_device, fake_clients):
    device = make_device(ip="10.100.1.14")
    add_log(db, device, pin="1001", card="2150141426")

    MonitorService(db).snapshot()

    assert fake_clients.created == []


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
def test_snapshot_reports_panel_health_and_the_polling_switch(db, make_device):
    make_device(name="IGD Kiri", ip="10.100.1.14", is_online=True, personnel_count=41)
    make_device(name="OK PETUGAS", ip="10.100.1.26", is_online=False, last_error="menolak koneksi")

    snapshot = MonitorService(db).snapshot()

    by_name = {item["name"]: item for item in snapshot["devices"]}
    assert by_name["IGD Kiri"]["is_online"] is True
    assert by_name["IGD Kiri"]["personnel_count"] == 41
    assert by_name["OK PETUGAS"]["is_online"] is False
    assert by_name["OK PETUGAS"]["last_error"] == "menolak koneksi"
    assert snapshot["stats"]["devices"] == 2
    assert snapshot["stats"]["online"] == 1
    assert snapshot["stats"]["offline"] == 1
    # The test environment runs with SCHEDULER_ENABLED=false, exactly like the dev
    # launcher. The page needs to see that, or a motionless feed reads as "broken".
    assert snapshot["polling"]["enabled"] is False
    assert snapshot["polling"]["log_interval_seconds"] > 0
    assert snapshot["polling"]["health_interval_seconds"] > 0


def test_snapshot_is_empty_and_harmless_on_a_fresh_database(db):
    snapshot = MonitorService(db).snapshot()

    assert snapshot["devices"] == []
    assert snapshot["events"] == []
    assert snapshot["cursor"] == 0
    assert snapshot["stats"]["devices"] == 0
    assert snapshot["stats"]["last_event_at"] is None


def test_only_the_verified_event_codes_get_an_outcome(db, make_device, make_person):
    """A wrong "diterima" on a denial is worse than no colour at all."""
    device = make_device(ip="10.100.1.14")
    make_person(employee_id="1001", name="Budi")
    add_log(db, device, pin="1001", card="2150141426", code=0, seconds=0)
    add_log(db, device, pin="0", card="4063788292", code=27, seconds=1)
    add_log(db, device, pin="1001", code=5, seconds=2)

    events = MonitorService(db).recent_events()
    by_code = {event["event_code"]: event for event in events}

    assert by_code[0]["outcome"] == "diterima"
    assert by_code[27]["outcome"] == "ditolak"
    assert by_code[5]["outcome"] == "lain"
    # The feed names the person and the panel; an id alone is no use to an operator.
    assert by_code[0]["person_name"] == "Budi"
    assert by_code[0]["device_name"] == "IGD Kiri"
    # Newest first, and the cursor is the newest id in the payload.
    assert events[0]["event_code"] == 5
    assert MonitorService(db).snapshot()["cursor"] == events[0]["id"]


def test_device_timestamps_are_marked_as_utc(db, make_device):
    """SQLite drops the offset, so the page would show UTC as if it were local.

    `last_seen_at`/`last_log_time` are written with `now(timezone.utc)`; without the
    marker a browser renders them as wall-clock and they read seven hours behind.
    """
    device = make_device(ip="10.100.1.14")
    device.last_seen_at = datetime(2026, 9, 18, 0, 35, 39)  # naive, as SQLite returns it
    device.last_log_time = datetime(2026, 9, 18, 0, 35, 39)
    db.commit()

    status = MonitorService(db).device_statuses()[0]

    assert status["last_seen_at"].utcoffset() == timedelta(0)
    assert status["last_log_time"].utcoffset() == timedelta(0)


def test_panel_event_times_are_left_alone(db, make_device):
    """The panel's clock is wall-clock where the door is.

    Converting it would invent an offset the panel never sent, which is worse than
    no conversion at all.
    """
    device = make_device(ip="10.100.1.14")
    row = add_log(db, device, pin="1001")

    event = MonitorService(db).recent_events()[0]

    assert event["event_time"] == row.event_time


def test_events_after_the_cursor_ignores_rows_already_sent(db, make_device):
    device = make_device(ip="10.100.1.14")
    first = add_log(db, device, pin="1001")
    service = MonitorService(db)

    assert service.events_after(first.id) == []

    add_log(db, device, pin="1002", seconds=3)

    assert [event["employee_id"] for event in service.events_after(first.id)] == ["1002"]


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------
def test_stream_opens_with_a_snapshot_then_sends_only_what_is_new(db, make_device):
    device = make_device(ip="10.100.1.14")
    add_log(db, device, pin="1001")

    stream = _monitor_events(interval=0, limit=50)
    try:
        first = json.loads(next(stream))
        assert first["type"] == "snapshot"
        assert [item["name"] for item in first["devices"]] == [device.name]
        assert [event["employee_id"] for event in first["events"]] == ["1001"]
        assert first["cursor"] == first["events"][0]["id"]

        add_log(db, device, pin="1002", seconds=5)
        second = json.loads(next(stream))

        assert second["type"] == "update"
        assert [event["employee_id"] for event in second["events"]] == ["1002"]
        # 23 panels resent every few seconds would re-render the grid under the
        # operator's cursor for no new information.
        assert "devices" not in second
    finally:
        stream.close()


def test_stream_sends_new_events_newest_first(db, make_device):
    """The feed prepends each batch, so an oldest-first batch would render upside down."""
    device = make_device(ip="10.100.1.14")

    stream = _monitor_events(interval=0, limit=50)
    try:
        next(stream)
        add_log(db, device, pin="1001", seconds=1)
        add_log(db, device, pin="1002", seconds=2)

        update = json.loads(next(stream))

        assert [event["employee_id"] for event in update["events"]] == ["1002", "1001"]
        assert update["cursor"] == update["events"][0]["id"]
    finally:
        stream.close()


def test_stream_still_answers_when_nothing_happened(db, make_device):
    make_device(ip="10.100.1.14")

    stream = _monitor_events(interval=0, limit=50)
    try:
        next(stream)
        idle = json.loads(next(stream))

        # A heartbeat is still a message: it is how the page knows the stream is
        # alive on a quiet night instead of having died.
        assert idle["type"] == "update"
        assert idle["events"] == []
        assert idle["stats"]["devices"] == 1
    finally:
        stream.close()


def test_stream_resends_the_panel_grid_when_a_panel_falls_offline(db, make_device):
    device = make_device(ip="10.100.1.14", is_online=True)

    stream = _monitor_events(interval=0, limit=50)
    try:
        next(stream)

        device.is_online = False
        device.last_error = "koneksi ditolak"
        db.commit()

        update = json.loads(next(stream))

        assert "devices" in update  # the operator has to see it go down
        assert update["devices"][0]["is_online"] is False
        assert update["devices"][0]["last_error"] == "koneksi ditolak"
    finally:
        stream.close()


def test_snapshot_can_be_limited_to_one_day(db, make_device):
    """The feed is about a day, not about "the last 50 rows ever"."""
    from datetime import time

    device = make_device(ip="10.100.1.14")
    add_log(db, device, pin="1001", code=0, seconds=0)
    add_log(db, device, pin="1002", code=0, seconds=86400)
    service = MonitorService(db)
    day = BASE_TIME.date()

    everything = service.snapshot()
    one_day = service.snapshot(
        since=datetime.combine(day, time.min), until=datetime.combine(day, time.max)
    )

    assert [event["employee_id"] for event in everything["events"]] == ["1002", "1001"]
    assert [event["employee_id"] for event in one_day["events"]] == ["1001"]
    # No window means "not asked for", which is different from a day with no activity.
    assert everything["stats"]["events_in_range"] is None
    assert one_day["stats"]["events_in_range"] == 1
    assert one_day["window"]["since"] == datetime.combine(day, time.min)
    assert everything["window"] == {"since": None, "until": None}


def test_stream_respects_the_date_window(db, make_device):
    """A windowed feed must never leak a row from another day into the panel."""
    from datetime import time

    device = make_device(ip="10.100.1.14")
    add_log(db, device, pin="1001", code=0, seconds=0)
    day = BASE_TIME.date()

    stream = _monitor_events(
        interval=0,
        limit=50,
        since=datetime.combine(day, time.min),
        until=datetime.combine(day, time.max),
    )
    try:
        next(stream)
        add_log(db, device, pin="9999", code=0, seconds=86400)  # tomorrow
        update = json.loads(next(stream))

        assert update["events"] == []
    finally:
        stream.close()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
def test_monitor_snapshot_endpoint(client, db, make_device):
    device = make_device(ip="10.100.1.14")
    add_log(db, device, pin="1001", card="2150141426")

    payload = client.get("/api/monitor/snapshot").json()

    assert payload["stats"]["devices"] == 1
    assert payload["events"][0]["employee_id"] == "1001"
    assert payload["events"][0]["outcome"] == "diterima"
    assert payload["polling"]["enabled"] is False
    assert payload["cursor"] == payload["events"][0]["id"]


def test_monitor_routes_are_exposed(client):
    schema = client.get("/openapi.json").json()

    assert "/api/monitor/snapshot" in schema["paths"]
    assert "/api/monitor/stream" in schema["paths"]
    assert schema["paths"]["/api/monitor/stream"]["get"]["tags"] == ["monitor"]
