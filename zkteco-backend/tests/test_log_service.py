"""Tests for log ingestion (idempotency), realtime events and fleet sync."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.log_service import LogService
from app.services.sync_service import SyncService

EVENT_TIME = datetime(2026, 9, 16, 8, 30, tzinfo=timezone.utc)


def _txn(pin: str, index: int, seconds: int = 0) -> dict:
    moment = EVENT_TIME + timedelta(seconds=seconds)
    return {
        "Pin": pin,
        "Verified": "1",
        "DoorID": "1",
        "EventType": "0",
        "InOutState": "2",
        "Time_second": int(moment.timestamp()),
        "Index": str(index),
    }


def test_pull_logs_stores_events(db, make_device, fake_clients):
    device = make_device(ip="10.100.1.14")
    fake_clients.transactions["10.100.1.14"] = [_txn("1001", 1), _txn("1002", 2)]

    result = LogService(db).pull_device_logs(device.id)

    assert result["fetched"] == 2
    assert result["inserted"] == 2
    assert result["error"] is None
    assert LogService(db).count() == 2

    db.refresh(device)
    assert device.last_log_time is not None


def test_pull_logs_is_idempotent(db, make_device, fake_clients):
    device = make_device(ip="10.100.1.14")
    fake_clients.transactions["10.100.1.14"] = [_txn("1001", 1), _txn("1002", 2)]

    service = LogService(db)
    service.pull_device_logs(device.id)
    second = service.pull_device_logs(device.id)

    assert second["inserted"] == 0
    assert second["duplicates"] == 2
    assert service.count() == 2


def test_pull_logs_records_error_without_raising(db, make_device, fake_clients):
    """An unreadable transaction table must not blow up the whole job."""
    device = make_device(ip="10.100.1.3")
    fake_clients.transactions.pop("10.100.1.3", None)

    result = LogService(db).pull_device_logs(device.id)

    assert result["inserted"] == 0
    assert result["error"]


def test_pull_without_a_window_still_stores_everything(db, make_device, fake_clients):
    """The scheduled poller passes no window, so the whole history keeps syncing."""
    device = make_device(ip="10.100.1.14")
    fake_clients.transactions["10.100.1.14"] = [_txn("1001", 1), _txn("1002", 2, seconds=86400)]

    result = LogService(db).pull_device_logs(device.id)

    assert result["fetched"] == 2
    assert result["inserted"] == 2
    assert result["skipped"] == 0


def test_pull_with_a_window_stores_only_rows_inside_it(db, make_device, fake_clients):
    """A panel hands over its whole buffer, so the window is applied on the way in.

    Without it, asking for one day would drag a busy panel's entire history into the
    database (57k rows on 10.100.1.3).
    """
    from app.services.log_service import _to_datetime

    device = make_device(ip="10.100.1.14")
    rows = [_txn("1001", 1), _txn("1002", 2, seconds=86400)]
    fake_clients.transactions["10.100.1.14"] = rows
    service = LogService(db)
    # What the panel timestamps become is the library's business; the window only has
    # to agree with whatever that produced.
    moments = sorted(_to_datetime(row["Time_second"]) for row in rows)
    assert moments[0] < moments[1]
    middle = moments[0] + (moments[1] - moments[0]) / 2

    result = service.pull_device_logs(device.id, since=middle)

    assert result["fetched"] == 2
    assert result["inserted"] == 1
    assert result["skipped"] == 1
    assert service.count() == 1


def test_wall_clock_keeps_a_bound_comparable_with_panel_times():
    """Panel times have no timezone, so an aware bound must be converted, not stripped
    literally (which would move the day by the local offset, 7 hours in WIB).
    """
    from app.services.log_service import wall_clock

    aware = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    assert wall_clock(aware) == aware.astimezone().replace(tzinfo=None)
    assert wall_clock(aware).tzinfo is None

    naive = datetime(2026, 9, 18, 0, 0)
    assert wall_clock(naive) is naive
    assert wall_clock(None) is None


def test_the_agent_path_reads_the_transaction_table_one_field_at_a_time(
    db, make_device, monkeypatch
):
    """This firmware refuses a multi-field GETDATA on `transaction` (SDK: `-2`), so the
    columns have to be fetched one per request and stitched back by row position.
    """
    from app.config import settings
    from app.services import push_agent
    from app.services.log_service import TRANSACTION_FIELDS

    device = make_device(ip="10.100.1.14")
    row = {
        "Pin": "1001",
        "Verified": "1",
        "DoorID": "1",
        "EventType": "0",
        "InOutState": "2",
        "Time_second": "1700000000",
        "Index": "7",
        "Cardno": "2150141426",
        "Sitecode": "0",
    }
    calls: list[list[str]] = []

    def fake(self, ip, table, fields=None, **kwargs):
        calls.append(list(fields))
        return [{fields[0]: row[fields[0]]}]

    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(push_agent.PushAgentClient, "read_table", fake)

    result = LogService(db).pull_device_logs(device.id)

    assert result["inserted"] == 1
    assert [fields[0] for fields in calls] == TRANSACTION_FIELDS  # one field per request
    stored = LogService(db).search()[0]
    assert stored.employee_id == "1001"
    assert stored.card_number == "2150141426"


def test_a_panel_that_shifts_mid_read_is_refused_not_mis_stitched(
    db, make_device, fake_clients, monkeypatch
):
    """A Pin from one read stitched onto a Cardno from another writes a wrong access
    log, which is worse than a failed pull.
    """
    from app.config import settings
    from app.services import push_agent

    device = make_device(ip="10.100.1.14")
    fake_clients.transactions.pop("10.100.1.14", None)

    def growing(self, ip, table, fields=None, **kwargs):
        # The panel's log grew between two field reads.
        return [{fields[0]: "1"}] * (1 if fields[0] == "Pin" else 2)

    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(push_agent.PushAgentClient, "read_table", growing)

    result = LogService(db).pull_device_logs(device.id)

    assert result["inserted"] == 0
    assert "mengubah isi log" in result["error"]


def test_realtime_events_are_normalised(db, make_device, fake_clients):
    device = make_device(ip="10.100.1.14")

    events = LogService(db).realtime_events(device.id)

    assert len(events) == 1
    event = events[0]
    assert event["device_id"] == device.id
    assert event["event_type"] == "NORMAL_PUNCH_OPEN"
    assert event["card_number"] == "12345"
    assert event["door_number"] == 1


def test_search_filters_by_device(db, make_device, fake_clients):
    first = make_device(name="A", ip="10.100.1.14")
    second = make_device(name="B", ip="10.100.1.15")
    fake_clients.transactions["10.100.1.14"] = [_txn("1001", 1)]
    fake_clients.transactions["10.100.1.15"] = [_txn("1002", 2)]

    service = LogService(db)
    service.pull_device_logs(first.id)
    service.pull_device_logs(second.id)

    assert len(service.search(device_id=first.id)) == 1
    assert len(service.search()) == 2
    assert service.search(employee_id="1002")[0].device_id == second.id


def test_pull_logs_prefers_the_agent(db, make_device, fake_clients, monkeypatch):
    """A busy panel's log can only be read through the SDK's tunable buffer.

    Measured on 10.100.1.3 (56,954 stored transactions): the library refuses the
    reply outright and has no buffer to raise, while the agent with a 4 MB buffer
    returns everything. So the agent has to be the first choice, not a fallback.
    """
    from app.config import settings
    from app.services import push_agent

    device = make_device(ip="10.100.1.14")
    fake_clients.transactions.pop("10.100.1.14", None)  # the library path would fail

    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(
        push_agent.PushAgentClient,
        "read_table",
        lambda self, ip, table, fields=None, **kwargs: [_txn("1001", 1)],
    )

    result = LogService(db).pull_device_logs(device.id)

    assert result["inserted"] == 1
    assert result["error"] is None
    assert fake_clients.created == []  # the library never touched the panel


def test_pull_logs_falls_back_to_the_library_when_the_agent_fails(
    db, make_device, fake_clients, monkeypatch
):
    """An agent that is up but cannot serve this panel must not lose the pull."""
    from app.config import settings
    from app.services import push_agent

    device = make_device(ip="10.100.1.14")
    fake_clients.transactions["10.100.1.14"] = [_txn("1001", 1)]
    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")

    def refuse(self, ip, table, fields=None, **kwargs):
        raise push_agent.PushAgentError("GetDeviceData(transaction) gagal (kode -112)")

    monkeypatch.setattr(push_agent.PushAgentClient, "read_table", refuse)

    result = LogService(db).pull_device_logs(device.id)

    assert result["inserted"] == 1
    assert result["error"] is None


def test_pull_error_names_the_agent_buffer_when_both_paths_fail(
    db, make_device, fake_clients, monkeypatch
):
    """Neither raw message says what to do: the library blames the table, the agent
    blames nothing. The real fix is a bigger agent buffer, so the error must say so.
    """
    from app.config import settings
    from app.services import push_agent

    device = make_device(ip="10.100.1.3")
    fake_clients.transactions.pop("10.100.1.3", None)
    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")

    def refuse(self, ip, table, fields=None, **kwargs):
        raise push_agent.PushAgentError("GetDeviceData(transaction) gagal (kode -112)")

    monkeypatch.setattr(push_agent.PushAgentClient, "read_table", refuse)

    result = LogService(db).pull_device_logs(device.id)

    assert result["inserted"] == 0
    assert "ZK_AGENT_BUFFER_SIZE" in result["error"]
    assert "restart agent" in result["error"]


def test_pull_error_points_at_the_agent_when_none_is_configured(db, make_device, fake_clients):
    """Without an agent a big log is unreadable, and the message has to say that
    instead of implying the panel is broken.
    """
    device = make_device(ip="10.100.1.3")
    fake_clients.transactions.pop("10.100.1.3", None)

    result = LogService(db).pull_device_logs(device.id)

    assert result["inserted"] == 0
    assert "PUSH_AGENT_URL" in result["error"]
    assert "ZK_AGENT_BUFFER_SIZE" in result["error"]


def test_pull_asks_the_agent_to_wait_for_a_big_table(db, make_device, monkeypatch):
    """A full transaction table takes long enough to stream that the agent's 4 s default
    connect timeout expires mid-transfer and the SDK answers `-2`.
    """
    from app.config import settings
    from app.services import push_agent

    device = make_device(ip="10.100.1.14")
    seen: dict = {}

    def fake(self, ip, table, fields=None, **kwargs):
        seen.update(kwargs)
        return [_txn("1001", 1)]

    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(push_agent.PushAgentClient, "read_table", fake)

    LogService(db).pull_device_logs(device.id)

    assert seen.get("timeout_ms", 0) >= 10000


def test_a_busy_panel_is_reported_as_busy_not_as_a_buffer_problem(
    db, make_device, fake_clients, monkeypatch
):
    """`-2` means busy/timeout, and the panel is fine.

    Telling the operator to change a buffer setting would send them the wrong way — and
    that is exactly what happened while diagnosing this: a timeout was read as a size
    problem for a while.
    """
    from app.config import settings
    from app.services import push_agent

    device = make_device(ip="10.100.1.3")
    fake_clients.transactions.pop("10.100.1.3", None)
    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")

    def refuse(self, ip, table, fields=None, **kwargs):
        raise push_agent.PushAgentError("GetDeviceData(transaction) gagal (kode -2)")

    monkeypatch.setattr(push_agent.PushAgentClient, "read_table", refuse)

    result = LogService(db).pull_device_logs(device.id)

    assert "sibuk" in result["error"]
    assert "ZK_AGENT_BUFFER_SIZE" not in result["error"]


def test_switching_read_paths_does_not_duplicate_the_log(
    db, make_device, fake_clients, monkeypatch
):
    """The same panel row must hash the same however it was read.

    The library's `transaction` read has no `Cardno` field at all, while the SDK
    returns `"0"` for a row with no card. Since the dedupe key is built from those
    values, that difference made one event hash two ways and stored it twice —
    measured on FARAMASI LOG: 586 rows inserted once per path.
    """
    from app.config import settings
    from app.services import push_agent

    device = make_device(ip="10.100.1.14")
    fake_clients.transactions["10.100.1.14"] = [_txn("1001", 1)]  # library shape
    service = LogService(db)

    assert service.pull_device_logs(device.id)["inserted"] == 1

    agent_row = _txn("1001", 1)
    agent_row["Cardno"] = "0"
    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(
        push_agent.PushAgentClient,
        "read_table",
        lambda self, ip, table, fields=None, **kwargs: [agent_row],
    )

    second = service.pull_device_logs(device.id)

    assert second["inserted"] == 0
    assert second["duplicates"] == 1
    assert service.count() == 1


def test_a_presented_card_is_still_stored(db, make_device, monkeypatch):
    """Collapsing the "no card" spellings must not eat a real card number.

    The card number only arrives over the agent path: this firmware's library read
    of `transaction` omits `Cardno` altogether.
    """
    from app.config import settings
    from app.services import push_agent

    device = make_device(ip="10.100.1.14")
    row = _txn("1001", 1)
    row["Cardno"] = "2150141426"
    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(
        push_agent.PushAgentClient,
        "read_table",
        lambda self, ip, table, fields=None, **kwargs: [row],
    )

    service = LogService(db)
    service.pull_device_logs(device.id)

    assert service.search()[0].card_number == "2150141426"


def test_a_row_without_a_card_is_stored_as_empty(db, make_device, monkeypatch):
    """The SDK's "0" means no card was presented, not card number zero."""
    from app.config import settings
    from app.services import push_agent

    device = make_device(ip="10.100.1.14")
    row = _txn("0", 1)
    row["Cardno"] = "0"
    monkeypatch.setattr(settings, "push_agent_url", "http://127.0.0.1:8081")
    monkeypatch.setattr(
        push_agent.PushAgentClient,
        "read_table",
        lambda self, ip, table, fields=None, **kwargs: [row],
    )

    service = LogService(db)
    service.pull_device_logs(device.id)

    assert service.search()[0].card_number is None


def test_fleet_refresh_isolates_failing_device(db, make_device, fake_clients):
    """One unreachable panel must not stop the others."""
    good = make_device(name="Good", ip="10.100.1.14")
    bad = make_device(name="Bad", ip="10.100.1.99")
    fake_clients.online["10.100.1.99"] = False
    fake_clients.users["10.100.1.14"] = [{"UID": "1"}, {"UID": "2"}]

    summary = SyncService(db).refresh_all()

    assert summary["total_devices"] == 2
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1

    db.refresh(good)
    db.refresh(bad)
    assert good.is_online is True
    assert bad.is_online is False


def test_fleet_refresh_can_target_specific_devices(db, make_device, fake_clients):
    first = make_device(name="A", ip="10.100.1.14")
    make_device(name="B", ip="10.100.1.15")

    summary = SyncService(db).refresh_all([first.id])

    assert summary["total_devices"] == 1
    assert summary["details"][0]["device_id"] == first.id


def test_fleet_pull_logs_reports_per_device(db, make_device, fake_clients):
    make_device(ip="10.100.1.14")
    fake_clients.transactions["10.100.1.14"] = [_txn("1001", 1)]

    summary = SyncService(db).pull_all_logs()

    assert summary["succeeded"] == 1
    assert summary["details"][0]["inserted"] == 1
