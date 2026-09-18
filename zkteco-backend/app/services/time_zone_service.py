"""Access-control time zones ("jam akses").

On the panels this is the `timezone` table; in the legacy ZKAccess database it
is `acc_timeseg`. A zone holds up to three segments per weekday.

The number the firmware uses is :attr:`AccessTimeZone.device_timezone_id`, and
``AccessGroup.device_timezone_id`` already points at it — so linking an access
level to a zone is a matter of setting that one number.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import AccessGroup, AccessTimeZone, AccessTimeZoneSlot
from app.schemas import TimeZoneCreate, TimeZoneSlotInput, TimeZoneUpdate

logger = logging.getLogger(__name__)

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
MAX_SLOTS = 3

#: The "24 Hours" preset, as it exists on these panels and in ZKAccess:
#: every day open 00:00-23:59, the other two segments unused.
HOURS_24_PRESET = {
    day: [("00:00", "23:59"), ("00:00", "00:00"), ("00:00", "00:00")] for day in range(len(DAYS))
}


class TimeZoneNotFoundError(LookupError):
    pass


class DuplicateTimeZoneError(ValueError):
    pass


class TimeZoneInUseError(ValueError):
    """Raised when deleting a zone that access levels still point at."""


def _with_slots():
    return selectinload(AccessTimeZone.slots)


class TimeZoneService:
    def __init__(self, db: Session):
        self.db = db

    # -- queries -----------------------------------------------------------
    def list(self) -> list[AccessTimeZone]:
        stmt = (
            select(AccessTimeZone)
            .options(_with_slots())
            .order_by(AccessTimeZone.device_timezone_id)
        )
        return list(self.db.scalars(stmt))

    def get(self, time_zone_id: int) -> AccessTimeZone:
        zone = self.db.scalars(
            select(AccessTimeZone)
            .options(_with_slots())
            .where(AccessTimeZone.id == time_zone_id)
            .execution_options(populate_existing=True)
        ).first()
        if zone is None:
            raise TimeZoneNotFoundError(f"Time zone {time_zone_id} tidak ditemukan")
        return zone

    def get_by_device_id(self, device_timezone_id: int) -> AccessTimeZone | None:
        return self.db.scalars(
            select(AccessTimeZone)
            .options(_with_slots())
            .where(AccessTimeZone.device_timezone_id == device_timezone_id)
        ).first()

    def for_group(self, group: AccessGroup) -> AccessTimeZone | None:
        """The zone an access level uses, via its `device_timezone_id`."""
        return self.get_by_device_id(group.device_timezone_id)

    def name_for_device_id(self, device_timezone_id: int | None) -> str | None:
        if device_timezone_id is None:
            return None
        zone = self.get_by_device_id(device_timezone_id)
        return zone.name if zone else None

    # -- writes ------------------------------------------------------------
    def create(self, payload: TimeZoneCreate) -> AccessTimeZone:
        zone = AccessTimeZone(
            name=payload.name,
            device_timezone_id=payload.device_timezone_id,
            description=payload.description,
            is_24_hour=payload.is_24_hour,
        )
        self.db.add(zone)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateTimeZoneError("Nama atau nomor time zone sudah dipakai") from exc

        self._replace_slots(zone, payload.slots)
        self.db.commit()
        return self.get(zone.id)

    def update(self, time_zone_id: int, payload: TimeZoneUpdate) -> AccessTimeZone:
        zone = self.get(time_zone_id)
        data = payload.model_dump(exclude_unset=True)
        slots = data.pop("slots", None)

        for field, value in data.items():
            setattr(zone, field, value)

        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateTimeZoneError("Nama atau nomor time zone sudah dipakai") from exc

        if slots is not None:
            self._replace_slots(zone, [TimeZoneSlotInput(**slot) for slot in slots])

        self.db.commit()
        return self.get(time_zone_id)

    def delete(self, time_zone_id: int) -> None:
        zone = self.get(time_zone_id)
        in_use = self.db.scalars(
            select(AccessGroup.id).where(AccessGroup.device_timezone_id == zone.device_timezone_id)
        ).first()
        if in_use is not None:
            raise TimeZoneInUseError(
                f"Time zone '{zone.name}' masih dipakai access level "
                f"(nomor {zone.device_timezone_id}); pindahkan dulu level-nya"
            )
        self.db.delete(zone)
        self.db.commit()

    def set_group_time_zone(self, group_id: int, time_zone_id: int) -> AccessGroup:
        """Point an access level at a zone (updates its `device_timezone_id`)."""
        zone = self.get(time_zone_id)
        group = self.db.get(AccessGroup, group_id)
        if group is None:
            raise TimeZoneNotFoundError(f"Access level {group_id} tidak ditemukan")

        group.device_timezone_id = zone.device_timezone_id
        self.db.commit()
        self.db.refresh(group)
        return group

    # -- presets -----------------------------------------------------------
    def ensure_24_hour(self, device_timezone_id: int = 1) -> AccessTimeZone:
        """Create (or return) the 24-hour zone used by every panel here."""
        existing = self.get_by_device_id(device_timezone_id)
        if existing is not None:
            return existing

        logger.info("seeding 24-hour time zone at slot %s", device_timezone_id)
        return self.create(
            TimeZoneCreate(
                name="24 Jam",
                device_timezone_id=device_timezone_id,
                description="Akses penuh 24 jam, setiap hari (00:00-23:59)",
                is_24_hour=True,
                slots=[
                    TimeZoneSlotInput(
                        day_of_week=day, slot=index + 1, start_time=start, end_time=end
                    )
                    for day, segments in HOURS_24_PRESET.items()
                    for index, (start, end) in enumerate(segments)
                ],
            )
        )

    def seed_presets(self) -> list[AccessTimeZone]:
        """Make sure the preset zones the panels expect exist."""
        before = {zone.id for zone in self.list()}
        self.ensure_24_hour(1)
        return [zone for zone in self.list() if zone.id not in before]

    # -- internals ---------------------------------------------------------
    def _replace_slots(self, zone: AccessTimeZone, slots: list[TimeZoneSlotInput]) -> None:
        seen: set[tuple[int, int]] = set()
        for slot in slots:
            key = (slot.day_of_week, slot.slot)
            if key in seen:
                raise DuplicateTimeZoneError(
                    f"Slot hari {DAYS[slot.day_of_week]} #{slot.slot} duplikat"
                )
            seen.add(key)

        for existing in list(zone.slots):
            self.db.delete(existing)
        self.db.flush()

        for slot in slots:
            self.db.add(
                AccessTimeZoneSlot(
                    time_zone_id=zone.id,
                    day_of_week=slot.day_of_week,
                    slot=slot.slot,
                    start_time=slot.start_time,
                    end_time=slot.end_time,
                )
            )
        self.db.flush()
