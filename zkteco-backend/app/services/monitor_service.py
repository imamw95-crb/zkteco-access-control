"""Live fleet monitoring: panel health and the access-event feed.

**Everything here reads the database only.** A panel accepts a single connection
at a time (`AGENTS.md` rule 12) and the scheduler, the dashboard and any push all
compete for it — so monitoring must never dial a panel itself. The scheduler
polls each panel and this service reports what that polling stored.

The consequence is worth stating out loud, because it is the difference between
a working feature and a page that looks broken: with ``SCHEDULER_ENABLED=false``
(what the dev launcher and the API systemd unit use) *nothing polls*, so the
feed stands still even though panels are busy. :meth:`MonitorService.polling_state`
exposes that switch so the page can say so, and the manual pull
(``POST /api/logs/pull``) is what fills the feed in that setup.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AccessLog, Device, Personnel
from app.services.log_service import wall_clock

#: The only two EventType codes verified on this fleet: 0 = NORMAL_PUNCH_OPEN (the
#: panel accepted the card) and 27 = UNREGISTERED_CARD (denied). Measured over the
#: 1000 newest stored rows: 811 accepted, 91 denied, and the remaining 98 were
#: operational events (DEVICE_START, WG_FORMAT_ERROR, REMOTE_OPENING). That is why
#: these two are worth classifying and the rest are not — a denial row also carries
#: the Cardno the reader *actually saw*, which is how a mis-encoded card is
#: diagnosed, so it must stand out rather than hide.
GRANTED_EVENT_CODES = frozenset({0})
DENIED_EVENT_CODES = frozenset({27})

OUTCOME_GRANTED = "diterima"
OUTCOME_DENIED = "ditolak"
OUTCOME_OTHER = "lain"

DEFAULT_FEED_LIMIT = 50
MAX_FEED_LIMIT = 200


class MonitorService:
    """Read-only views for the Monitoring tab."""

    def __init__(self, db: Session):
        self.db = db

    # -- polling state -----------------------------------------------------
    def polling_state(self) -> dict:
        """Whether anything is polling the panels at all.

        Without this the page cannot tell "no one is walking through a door"
        apart from "the log poller is switched off".
        """
        return {
            "enabled": bool(settings.scheduler_enabled),
            "log_interval_seconds": int(settings.log_poll_interval_seconds),
            "health_interval_seconds": int(settings.health_check_interval_seconds),
        }

    # -- devices -----------------------------------------------------------
    def device_statuses(self) -> list[dict]:
        devices = self.db.scalars(select(Device).order_by(Device.name)).all()
        return [self._device_dict(device) for device in devices]

    @staticmethod
    def fingerprint(devices: list[dict]) -> tuple:
        """Cheap change-detector so an idle stream does not resend 23 panels.

        The stream sends the device grid only when this changes; the rest of the
        time the browser keeps the table it already rendered (no flicker, no
        losing the operator's scroll position).
        """
        return tuple(
            (
                item["id"],
                item["is_online"],
                item["last_seen_at"],
                item["last_log_time"],
                item["last_error"],
                item["personnel_count"],
            )
            for item in devices
        )

    def _device_dict(self, device: Device) -> dict:
        return {
            "id": device.id,
            "name": device.name,
            "ip": device.ip,
            "port": device.port,
            "location": device.location,
            "area": device.area,
            "is_active": device.is_active,
            "is_online": device.is_online,
            # SQLite drops the offset these were written with, so without this they
            # reach the page naive and are rendered as local wall-clock — seven hours
            # behind in WIB. `event_time` is deliberately NOT treated this way.
            "last_seen_at": _as_utc(device.last_seen_at),
            "last_log_time": _as_utc(device.last_log_time),
            "last_error": device.last_error,
            "personnel_count": device.personnel_count,
            "lock_count": device.lock_count,
        }

    # -- events ------------------------------------------------------------
    def recent_events(
        self,
        *,
        limit: int = DEFAULT_FEED_LIMIT,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        """Newest ``limit`` events in the window, **newest first** (ready for the feed)."""
        limit = _clamp_limit(limit)
        stmt = _in_window(select(AccessLog), since, until)
        rows = list(self.db.scalars(stmt.order_by(AccessLog.id.desc()).limit(limit)))
        return self._enrich(rows)

    def events_after(
        self,
        cursor: int,
        *,
        limit: int = DEFAULT_FEED_LIMIT,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        """Events stored after ``cursor``, **oldest first** so the feed can prepend.

        ``AccessLog.id`` is a monotonically increasing primary key, so this is an
        index-only range scan — the whole reason the feed can be polled cheaply.
        """
        limit = _clamp_limit(limit)
        stmt = _in_window(select(AccessLog).where(AccessLog.id > int(cursor or 0)), since, until)
        rows = list(self.db.scalars(stmt.order_by(AccessLog.id.asc()).limit(limit)))
        return self._enrich(rows)

    def latest_cursor(self) -> int:
        return int(self.db.scalar(select(func.max(AccessLog.id))) or 0)

    def _enrich(self, rows: list[AccessLog]) -> list[dict]:
        """Resolve panel and person names in two bulk queries, never per row."""
        device_names: dict[int, str] = {}
        person_names: dict[str, str] = {}
        if rows:
            device_ids = {row.device_id for row in rows}
            device_names = dict(
                self.db.execute(
                    select(Device.id, Device.name).where(Device.id.in_(device_ids))
                ).all()
            )
            pins = {row.employee_id for row in rows if row.employee_id}
            if pins:
                person_names = dict(
                    self.db.execute(
                        select(Personnel.employee_id, Personnel.name).where(
                            Personnel.employee_id.in_(pins)
                        )
                    ).all()
                )
        return [self._event_dict(row, device_names, person_names) for row in rows]

    def _event_dict(
        self, row: AccessLog, device_names: dict[int, str], person_names: dict[str, str]
    ) -> dict:
        return {
            "id": row.id,
            "device_id": row.device_id,
            "device_name": device_names.get(row.device_id),
            "event_time": row.event_time,
            "event_code": row.event_code,
            "event_type": row.event_type,
            "card_number": row.card_number,
            "employee_id": row.employee_id,
            "person_name": person_names.get(row.employee_id) if row.employee_id else None,
            "door_number": row.door_number,
            "verification_mode": row.verification_mode,
            "in_out_status": row.in_out_status,
            "outcome": _outcome(row.event_code),
        }

    # -- aggregates --------------------------------------------------------
    def stats(self, *, since: datetime | None = None, until: datetime | None = None) -> dict:
        since, until = wall_clock(since), wall_clock(until)
        devices = int(self.db.scalar(select(func.count()).select_from(Device)) or 0)
        online = int(
            self.db.scalar(
                select(func.count()).select_from(Device).where(Device.is_online.is_(True))
            )
            or 0
        )
        # Only computed when a window is asked for: it is the number that answers
        # "how busy was today", and the feed itself is capped at 200 rows.
        in_range = None
        if since is not None or until is not None:
            counter = _in_window(select(func.count()).select_from(AccessLog), since, until)
            in_range = int(self.db.scalar(counter) or 0)
        return {
            "devices": devices,
            "online": online,
            "offline": max(devices - online, 0),
            "log_rows": int(self.db.scalar(select(func.count()).select_from(AccessLog)) or 0),
            "events_in_range": in_range,
            "last_event_at": self.db.scalar(select(func.max(AccessLog.event_time))),
        }

    # -- one-shot snapshot -------------------------------------------------
    def snapshot(
        self,
        *,
        limit: int = DEFAULT_FEED_LIMIT,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict:
        """Everything the page needs to draw itself once, without a stream."""
        since, until = wall_clock(since), wall_clock(until)
        return {
            "generated_at": datetime.now(timezone.utc),
            "polling": self.polling_state(),
            "stats": self.stats(since=since, until=until),
            "devices": self.device_statuses(),
            "events": self.recent_events(limit=limit, since=since, until=until),
            "cursor": self.latest_cursor(),
            "window": {"since": since, "until": until},
        }


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_FEED_LIMIT))


def _in_window(stmt, since: datetime | None, until: datetime | None):
    """Apply a wall-clock date window to an `AccessLog` query."""
    since, until = wall_clock(since), wall_clock(until)
    if since is not None:
        stmt = stmt.where(AccessLog.event_time >= since)
    if until is not None:
        stmt = stmt.where(AccessLog.event_time <= until)
    return stmt


def _as_utc(value: datetime | None) -> datetime | None:
    """Restore the UTC marker on a timestamp the database stored without one.

    ``Device.last_seen_at`` / ``last_log_time`` are written with ``now(timezone.utc)``
    but SQLite's DATETIME column has nowhere to keep the offset, so they come back
    naive and a browser reads them as local time. Only these fields are converted:
    ``AccessLog.event_time`` is the panel's own clock, i.e. wall-clock where the door
    is, and stamping UTC on it would invent an offset the panel never sent.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _outcome(event_code: int | None) -> str:
    """Map a panel EventType onto what the operator needs to see.

    Only the two codes verified on this fleet are classified; anything else stays
    "lain" rather than being guessed at, because a wrong "diterima" on a denial
    is worse than no colour at all.
    """
    if event_code in GRANTED_EVENT_CODES:
        return OUTCOME_GRANTED
    if event_code in DENIED_EVENT_CODES:
        return OUTCOME_DENIED
    return OUTCOME_OTHER
