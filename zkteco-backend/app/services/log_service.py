"""Access log ingestion (idempotent) and realtime event monitoring."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccessLog, Device
from app.services.device_client import DeviceEvent
from app.services.device_service import DeviceService
from app.services.push_agent import PushAgentClient, PushAgentError

logger = logging.getLogger(__name__)

#: Field list of the `transaction` table on C3 panels.
TRANSACTION_FIELDS = [
    "Pin",
    "Verified",
    "DoorID",
    "EventType",
    "InOutState",
    "Time_second",
    "Index",
    "Cardno",
    "Sitecode",
]

#: Connect timeout for reading that table through the agent. A busy panel's log is
#: several megabytes (57k rows on 10.100.1.3) and the agent's 4 s default expires
#: mid-transfer, which the SDK reports as `-2` — indistinguishable from a panel that
#: is genuinely busy, and it sent us chasing the wrong problem for a while.
BIG_TABLE_TIMEOUT_MS = 15000


class LogService:
    def __init__(self, db: Session):
        self.db = db
        self.devices = DeviceService(db)

    # -- pulling -----------------------------------------------------------
    def pull_device_logs(
        self,
        device_id: int,
        *,
        limit: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict:
        """Pull the transaction table from one panel and store new rows.

        Writing is idempotent: each row gets a deterministic ``dedupe_key``, and
        rows whose key already exists are skipped, so polling repeatedly never
        duplicates history.

        ``since``/``until`` narrow **what gets stored**, not what is read: a panel
        answers a transaction request with its whole buffer or nothing at all, so a
        busy panel still sends ~57,000 rows and the reply still has to fit the agent
        buffer. Rows outside the window are reported as ``skipped`` so an empty
        result is never mistaken for a failed read.
        """
        device = self.devices.get(device_id)
        records, error = self._read_transactions(device)
        if error:
            logger.warning("log pull failed for %s (%s): %s", device.name, device.ip, error)
            return {
                "device_id": device_id,
                "fetched": 0,
                "inserted": 0,
                "duplicates": 0,
                "skipped": 0,
                "error": error,
            }

        fetched = len(records)
        window = (wall_clock(since), wall_clock(until))
        if window != (None, None):
            records = [row for row in records if _inside_window(row, window)]

        inserted, duplicates = self._store(device, records, limit=limit)
        device.last_log_time = datetime.now(timezone.utc)
        self.db.commit()
        return {
            "device_id": device_id,
            "fetched": fetched,
            "inserted": inserted,
            "duplicates": duplicates,
            "skipped": fetched - len(records),
            "error": None,
        }

    def _read_transactions(self, device: Device) -> tuple[list[dict], str | None]:
        """Read the panel's ``transaction`` table, preferring the Windows agent.

        The library path cannot read a busy panel's log at all. A full transaction
        table is several megabytes, and the panel answers the request with a refusal
        payload that the library reports as ``Wrong table returned by panel`` — and
        unlike the agent there is no buffer size to raise on that path. The official
        Pull SDK behind the agent accepts one (``ZK_AGENT_BUFFER_SIZE``), so it is the
        only way in on those panels.

        Measured 2026-09-18 on ``10.100.1.3`` (56,954 stored transactions): the library
        refused, the agent with a 4 MB buffer returned all 56,954 records. On a panel
        whose log is small (``10.100.1.21``, 586 rows) both paths work.

        The library is kept as the fallback: it needs no agent and is what the tests
        and an agent-less deployment use.
        """
        agent = PushAgentClient()
        agent_error: str | None = None
        if agent.configured:
            try:
                return _read_transaction_fields(agent, device.ip), None
            except PushAgentError as exc:
                agent_error = str(exc)
                logger.info("agent read of %s failed, trying the library: %s", device.ip, exc)

        try:
            with self.devices.client_for(device) as client:
                return client.read_table("transaction"), None
        except Exception as exc:
            return [], self._read_error(device, agent_error, exc)

    @staticmethod
    def _read_error(device: Device, agent_error: str | None, library_error: Exception) -> str:
        """Say which knob to turn, because neither message does on its own.

        The library's own error ("Wrong table returned by panel") reads like a bug in
        our code, and the agent's (`-112`) reads like a broken panel. Both actually
        mean "this panel's log is bigger than the buffer", and only the agent has a
        buffer to change.
        """
        if agent_error:
            if "-112" in agent_error:
                return (
                    f"Log {device.ip} tidak terbaca: balasan panel lebih besar dari buffer "
                    f"agent ({agent_error}). Naikkan ZK_AGENT_BUFFER_SIZE (mis. 4194304) lalu "
                    "restart agent; jalur library tidak punya buffer yang bisa dinaikkan."
                )
            if "-2" in agent_error or "-107" in agent_error:
                # Retried and still busy: say so, because the panel is fine and telling the
                # operator to change a buffer setting would send them the wrong way.
                return (
                    f"Panel {device.ip} menjawab sibuk/timeout ({agent_error}). Panel C3 hanya "
                    "menerima satu koneksi sekaligus, dan log besar butuh waktu — tunggu "
                    "sebentar lalu ulangi, dan pastikan tidak ada dashboard atau scheduler "
                    "yang membaca panel yang sama."
                )
            return (
                f"Log {device.ip} tidak terbaca lewat agent ({agent_error}) maupun library "
                f"({library_error})."
            )
        return (
            f"Log {device.ip} tidak terbaca lewat library ({library_error}). Panel dengan "
            "log besar hanya bisa dibaca lewat agent Windows: isi PUSH_AGENT_URL dan "
            "naikkan ZK_AGENT_BUFFER_SIZE (mis. 4194304)."
        )

    def _store(
        self, device: Device, records: list[dict], *, limit: int | None = None
    ) -> tuple[int, int]:
        inserted = duplicates = 0
        for rec in records[:limit] if limit else records:
            event_time = _to_datetime(rec.get("Time_second"))
            key = _dedupe_key(
                device_serial=device.serial_number or device.ip,
                event_time=event_time,
                pin=rec.get("Pin"),
                card=_card_or_none(rec.get("Cardno")),
                event_code=rec.get("EventType"),
                door=rec.get("DoorID"),
                index=rec.get("Index"),
            )
            exists = self.db.scalars(
                select(AccessLog.id).where(AccessLog.dedupe_key == key)
            ).first()
            if exists:
                duplicates += 1
                continue

            self.db.add(
                AccessLog(
                    device_id=device.id,
                    device_serial=device.serial_number,
                    event_time=event_time or datetime.now(timezone.utc),
                    event_code=_to_int(rec.get("EventType")),
                    event_type=_event_type_name(rec.get("EventType")),
                    card_number=_card_or_none(rec.get("Cardno")),
                    employee_id=_text(rec.get("Pin")),
                    door_number=_to_int(rec.get("DoorID")),
                    verification_mode=_verification_name(rec.get("Verified")),
                    in_out_status=_text(rec.get("InOutState")),
                    dedupe_key=key,
                    raw=_json(rec),
                )
            )
            inserted += 1

        self.db.flush()
        return inserted, duplicates

    # -- realtime ----------------------------------------------------------
    def realtime_events(self, device_id: int) -> list[dict]:
        """Drain the realtime buffer of a panel (live door open/close events)."""
        device = self.devices.get(device_id)
        client = self.devices.client_for(device)
        try:
            client.connect()
            events: list[DeviceEvent] = client.poll_events()
        finally:
            client.disconnect()
        return [self._event_to_dict(device_id, ev) for ev in events]

    def _event_to_dict(self, device_id: int, ev: DeviceEvent) -> dict:
        return {
            "device_id": device_id,
            "record_type": ev.record_type,
            "event_time": ev.event_time,
            "event_code": ev.event_code,
            "event_type": ev.event_type,
            "card_number": ev.card_number,
            "door_number": ev.door_number,
            "raw": ev.raw,
        }

    # -- querying ----------------------------------------------------------
    def search(
        self,
        *,
        device_id: int | None = None,
        employee_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[AccessLog]:
        stmt = select(AccessLog).order_by(AccessLog.event_time.desc())
        if device_id is not None:
            stmt = stmt.where(AccessLog.device_id == device_id)
        if employee_id:
            stmt = stmt.where(AccessLog.employee_id == employee_id)
        if since:
            stmt = stmt.where(AccessLog.event_time >= since)
        if until:
            stmt = stmt.where(AccessLog.event_time <= until)
        return list(self.db.scalars(stmt.limit(limit).offset(offset)))

    def count(self) -> int:
        from sqlalchemy import func

        return int(self.db.scalar(select(func.count()).select_from(AccessLog)) or 0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _dedupe_key(**parts) -> str:
    material = "|".join(f"{k}={parts[k]}" for k in sorted(parts))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_transaction_fields(agent: PushAgentClient, ip: str) -> list[dict]:
    """Read the `transaction` table one field per request and stitch it back together.

    This firmware refuses a **multi-field** GETDATA on `transaction`: the SDK answers
    `-2` and the library reports "Wrong table returned by panel". A single field is
    served happily — measured 2026-09-18 on 10.100.1.3, where 2, 4 and 9 fields were all
    refused while `Pin` alone came back with 56,960 rows in 8.6 s. So the columns are
    fetched one at a time and merged **by row position**, the same compromise
    `c3_compat.read_table_robust` makes on the library side.

    Positional merging is only sound while every read returns the same rows in the same
    order, so a row-count mismatch is raised instead of papered over: stitching a Pin
    from one read onto a Cardno from another would write a wrong access log, and a wrong
    log is worse than a failed pull. A panel whose buffer is full can shift between
    reads; the caller then sees an error and can simply retry.
    """
    per_field: dict[str, list[dict]] = {}
    for field in TRANSACTION_FIELDS:
        per_field[field] = agent.read_table(
            ip, "transaction", [field], timeout_ms=BIG_TABLE_TIMEOUT_MS
        )

    counts = {field: len(rows) for field, rows in per_field.items()}
    if len(set(counts.values())) > 1:
        raise PushAgentError(
            f"Panel {ip} mengubah isi log saat sedang dibaca ({counts}) — "
            "data tidak disimpan, ulangi sebentar lagi"
        )

    length = next(iter(counts.values()), 0)
    return [
        {field: per_field[field][position].get(field) for field in TRANSACTION_FIELDS}
        for position in range(length)
    ]


def wall_clock(value: datetime | None) -> datetime | None:
    """Drop the offset from a date bound so it can be compared with panel times.

    `AccessLog.event_time` is the panel's **own clock with no timezone** (and SQLite
    keeps no offset anyway), so a bound arriving from a browser as ``...Z`` would be
    both uncomparable (Python refuses aware vs naive) and wrong: converting it would
    move the day boundary by seven hours in WIB. An aware bound is therefore read as
    the local wall-clock time it names.

    Every panel-facing date filter goes through here — `LogService.pull_device_logs`
    and `MonitorService` alike — so both agree on where a day starts.
    """
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


def _inside_window(row: dict, window: tuple[datetime | None, datetime | None]) -> bool:
    since, until = window
    moment = _to_datetime(row.get("Time_second"))
    if moment is None:
        # An unparsable timestamp cannot be placed in a window, and storing it as
        # "now" (what _store does without a window) would drop it on the wrong day.
        return False
    if since is not None and moment < since:
        return False
    if until is not None and moment > until:
        return False
    return True


def _card_or_none(value) -> str | None:
    """Card number, with "no card" spelled one way only.

    The two read paths disagree about this field: the library's ``transaction`` read
    omits ``Cardno`` entirely, while the SDK behind the agent returns ``"0"`` for a
    row where no card was presented. Because ``dedupe_key`` is built from these
    values, that difference made the *same* event hash two ways — so switching paths
    (which is what happens the moment the agent is configured) stored a second copy
    of the panel's whole log. Measured 2026-09-18 on FARAMASI LOG: 586 rows inserted
    twice, once per path.

    A real card number is untouched; only the "no card" spellings collapse.
    """
    text = _text(value)
    return None if text in (None, "0") else text


def _to_datetime(value) -> datetime | None:
    """Panel timestamps are C3 epoch values or already datetimes."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    seconds = _to_int(value)
    if not seconds or seconds <= 0:
        return None
    try:
        from c3.rtlog import C3DateTime

        return C3DateTime.from_value(seconds)
    except Exception:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _event_type_name(value) -> str | None:
    code = _to_int(value)
    if code is None:
        return None
    try:
        from c3.consts import EventType

        return EventType(code).name
    except Exception:
        return f"EVENT_{code}"


def _verification_name(value) -> str | None:
    if value is None:
        return None
    try:
        from c3.consts import VerificationMode

        return VerificationMode(_to_int(value)).name
    except Exception:
        return str(value)


def _json(rec: dict) -> str:
    import json

    return json.dumps(rec, default=str)
