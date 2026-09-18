"""SQLAlchemy ORM models.

Central database that replaces the ZKAccess 3.5 database, with no door/user
limits. Only `device_id`+`serial_number` metadata is stored about panels; the
personnel master data lives here and is pushed down to devices on demand.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

#: The two dashboard roles. `admin` may open every tab and every setting; `hr`
#: only the people side (personnel, departments, access levels). The difference is
#: which part of the dashboard a person may use, never how much of the fleet.
ROLE_ADMIN = "admin"
ROLE_HR = "hr"
ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_HR)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AccessGroup(TimestampMixin, Base):
    """Logical grouping of personnel that maps to a device access group."""

    __tablename__ = "access_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Device-side access group / timezone id the group maps to.
    device_timezone_id: Mapped[int] = mapped_column(Integer, default=1)
    # Comma separated door numbers this group can open, e.g. "1,2".
    door_numbers: Mapped[str] = mapped_column(String(128), default="1")

    personnel: Mapped[list[Personnel]] = relationship(back_populates="access_group")
    doors: Mapped[list[AccessGroupDoor]] = relationship(
        back_populates="access_group", cascade="all, delete-orphan"
    )
    member_links: Mapped[list[PersonnelAccessGroup]] = relationship(
        back_populates="access_group", cascade="all, delete-orphan"
    )

    def door_list(self) -> list[int]:
        return [int(d) for d in self.door_numbers.split(",") if d.strip().isdigit()]

    @property
    def door_count(self) -> int:
        return len(self.doors)

    @property
    def member_count(self) -> int:
        return len(self.member_links)


class AccessGroupDoor(Base):
    """Which (device, door) an access group covers.

    A single group routinely spans many panels — the "TEKNISI/IT" group in the
    legacy ZKAccess data covers 22 doors across 20 devices — so this cannot be
    represented by the comma-separated ``AccessGroup.door_numbers`` shortcut.
    This table is the authoritative mapping.
    """

    __tablename__ = "access_group_doors"
    __table_args__ = (
        UniqueConstraint(
            "access_group_id", "device_id", "door_number", name="uq_access_group_door"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_group_id: Mapped[int] = mapped_column(
        ForeignKey("access_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    door_number: Mapped[int] = mapped_column(Integer, default=1)

    access_group: Mapped[AccessGroup] = relationship(back_populates="doors")
    device: Mapped[Device] = relationship()

    @property
    def device_name(self) -> str | None:
        return self.device.name if self.device is not None else None

    @property
    def device_ip(self) -> str | None:
        return self.device.ip if self.device is not None else None


class AccessTimeZone(TimestampMixin, Base):
    """An access-control time zone ("jam akses").

    On the panel this is the `timezone` table; in ZKAccess it is `acc_timeseg`.
    A zone holds up to three time segments per weekday, which is what the
    firmware supports.

    ``device_timezone_id`` is the number the panel and ZKAccess use to refer to
    this zone. ``AccessGroup.device_timezone_id`` points at it, so a group does
    not need a second foreign key; the lookup lives in
    ``TimeZoneService.for_group()``.
    """

    __tablename__ = "access_time_zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    #: The slot number the panel uses (ZKAccess `acc_timeseg.id`).
    device_timezone_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: True when every day is open around the clock (the "24 jam" preset).
    is_24_hour: Mapped[bool] = mapped_column(Boolean, default=False)

    slots: Mapped[list[AccessTimeZoneSlot]] = relationship(
        back_populates="time_zone", cascade="all, delete-orphan"
    )

    def active_slots(self, day_of_week: int) -> list[AccessTimeZoneSlot]:
        return sorted(
            (slot for slot in self.slots if slot.day_of_week == day_of_week and not slot.is_empty),
            key=lambda slot: slot.slot,
        )


class AccessTimeZoneSlot(Base):
    """One `start`-`end` segment of an access time zone.

    ``day_of_week`` follows the device order: **0 = Sunday** .. 6 = Saturday,
    matching the panel's ``SunTime1``..``SatTime1`` fields. (This differs from
    `DoorSchedule.day_of_week`, which is Monday-based; the device order is used
    here because this table mirrors the firmware.)

    A segment whose start equals its end is unused — the firmware represents an
    empty slot as ``00:00``-``00:00``.
    """

    __tablename__ = "access_time_zone_slots"
    __table_args__ = (
        UniqueConstraint("time_zone_id", "day_of_week", "slot", name="uq_time_zone_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    time_zone_id: Mapped[int] = mapped_column(
        ForeignKey("access_time_zones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    start_time: Mapped[str] = mapped_column(String(5), nullable=False, default="00:00")
    end_time: Mapped[str] = mapped_column(String(5), nullable=False, default="00:00")

    time_zone: Mapped[AccessTimeZone] = relationship(back_populates="slots")

    @property
    def is_empty(self) -> bool:
        return self.start_time == self.end_time

    @property
    def from_device_value(self) -> int | None:
        """Segment start as the firmware stores it (HHMM as an int)."""
        return _hhmm_to_int(self.start_time)

    @property
    def to_device_value(self) -> int | None:
        return _hhmm_to_int(self.end_time)


def _hhmm_to_int(value: str) -> int | None:
    try:
        hours, minutes = value.split(":")
        return int(hours) * 100 + int(minutes)
    except (AttributeError, ValueError):
        return None


class Department(TimestampMixin, Base):
    """Organisational unit, mirroring ZKAccess ``DEPARTMENTS``.

    ``legacy_id`` keeps the ZKAccess ``DEPTID`` so a re-run of the migration
    matches the same rows instead of recreating them.
    """

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    #: ZKAccess DEPTID (also the number the HR side uses).
    legacy_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    code: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)

    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), index=True
    )
    parent: Mapped[Department | None] = relationship(
        back_populates="children", remote_side="Department.id"
    )
    children: Mapped[list[Department]] = relationship(back_populates="parent")

    personnel: Mapped[list[Personnel]] = relationship(back_populates="department_ref")

    @property
    def full_path(self) -> str:
        """`Parent / Child` style name, following the hierarchy upwards."""
        parts: list[str] = []
        node: Department | None = self
        seen: set[int] = set()
        while node is not None and node.id not in seen:
            seen.add(node.id)
            parts.append(node.name)
            node = node.parent
        return " / ".join(reversed(parts))


class Device(TimestampMixin, Base):
    """A ZKTeco C3-100/200/300/400 access control panel."""

    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("ip", "port", name="uq_device_ip_port"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=4370)
    password: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[str | None] = mapped_column(String(128))
    area: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Cached "Get Information of Device" results ---------------------
    model: Mapped[str | None] = mapped_column(String(64))
    serial_number: Mapped[str | None] = mapped_column(String(64), index=True)
    firmware_version: Mapped[str | None] = mapped_column(String(128))
    mac_address: Mapped[str | None] = mapped_column(String(32))
    lock_count: Mapped[int | None] = mapped_column(Integer)
    reader_count: Mapped[int | None] = mapped_column(Integer)
    max_user_count: Mapped[int | None] = mapped_column(Integer)

    # --- Live health state ----------------------------------------------
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Actual number of personnel stored on the device (from the `user` table).
    personnel_count: Mapped[int | None] = mapped_column(Integer)
    # Timestamp of the newest transaction pulled from this device.
    last_log_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    personnel: Mapped[list[Personnel]] = relationship(back_populates="device")
    logs: Mapped[list[AccessLog]] = relationship(back_populates="device")
    schedules: Mapped[list[DoorSchedule]] = relationship(back_populates="device")

    @property
    def endpoint(self) -> str:
        return f"{self.ip}:{self.port}"


class Personnel(TimestampMixin, Base):
    """Central master record for a person that can be pushed to any device."""

    __tablename__ = "personnel"
    __table_args__ = (
        UniqueConstraint("employee_id", name="uq_personnel_employee_id"),
        UniqueConstraint("card_number", name="uq_personnel_card_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # User id on the device (also used as the device `user.PIN`).
    employee_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    card_number: Mapped[str | None] = mapped_column(String(32))
    pin: Mapped[str | None] = mapped_column(String(32))

    #: Department is a real master record now. `department` below is a read-only
    #: view of `department_ref.name`, so the API keeps returning the same field
    #: while there is only one place the data actually lives.
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), index=True
    )
    department_ref: Mapped[Department | None] = relationship(back_populates="personnel")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    access_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("access_groups.id", ondelete="SET NULL")
    )
    access_group: Mapped[AccessGroup | None] = relationship(back_populates="personnel")

    #: Authoritative group membership. In the legacy data 527 people held 2,037
    #: memberships (up to 11 groups each, 3.87 on average), so a single FK
    #: cannot express it. ``access_group_id`` above is kept only for
    #: convenience/back-compat and must not be used for access decisions.
    group_links: Mapped[list[PersonnelAccessGroup]] = relationship(
        back_populates="personnel", cascade="all, delete-orphan"
    )

    # When set, this record is pinned to a specific device only.
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    device: Mapped[Device | None] = relationship(back_populates="personnel")

    @property
    def access_group_names(self) -> list[str]:
        """Names of every access group this person belongs to.

        Read from ``group_links`` (the authoritative many-to-many), not from the
        legacy ``access_group_id`` column.
        """
        return sorted(
            link.access_group.name for link in self.group_links if link.access_group is not None
        )

    @property
    def access_group_ids(self) -> list[int]:
        """Ids of every access group this person belongs to.

        The edit form needs the ids to tick the right boxes; matching on names
        would work today (names are unique) but breaks the moment a rename is
        half-applied.
        """
        return sorted(link.access_group_id for link in self.group_links)

    @property
    def department(self) -> str | None:
        """Department name, read from the master record.

        Kept as a property (not a column) so the API response shape is unchanged
        while the data lives only in the `departments` table — renaming a
        department then updates every person at once.
        """
        return self.department_ref.name if self.department_ref is not None else None


class PersonnelAccessGroup(Base):
    """Many-to-many membership between a person and an access group."""

    __tablename__ = "personnel_access_groups"
    __table_args__ = (
        UniqueConstraint("personnel_id", "access_group_id", name="uq_personnel_access_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    personnel_id: Mapped[int] = mapped_column(
        ForeignKey("personnel.id", ondelete="CASCADE"), nullable=False, index=True
    )
    access_group_id: Mapped[int] = mapped_column(
        ForeignKey("access_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )

    personnel: Mapped[Personnel] = relationship(back_populates="group_links")
    access_group: Mapped[AccessGroup] = relationship(back_populates="member_links")


class AccessLog(Base):
    """Immutable audit trail of device transactions.

    `dedupe_key` is a deterministic hash over the identifying fields of the
    event, which makes repeated pulls idempotent.
    """

    __tablename__ = "access_logs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_access_log_dedupe"),
        Index("ix_access_logs_device_time", "device_id", "event_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    device_serial: Mapped[str | None] = mapped_column(String(64))

    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_code: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str | None] = mapped_column(String(128))
    card_number: Mapped[str | None] = mapped_column(String(32), index=True)
    employee_id: Mapped[str | None] = mapped_column(String(32), index=True)
    door_number: Mapped[int | None] = mapped_column(Integer)
    verification_mode: Mapped[str | None] = mapped_column(String(64))
    in_out_status: Mapped[str | None] = mapped_column(String(32))

    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    raw: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    device: Mapped[Device] = relationship(back_populates="logs")


class DoorSchedule(TimestampMixin, Base):
    """Access schedule (timezone) applied to a door of a device."""

    __tablename__ = "door_schedules"
    __table_args__ = (
        UniqueConstraint("device_id", "door_number", "day_of_week", name="uq_door_schedule"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    door_number: Mapped[int] = mapped_column(Integer, default=1)
    # 0=Monday .. 6=Sunday, 7=holiday
    day_of_week: Mapped[int] = mapped_column(Integer, default=0)
    start_time: Mapped[str] = mapped_column(String(5), default="00:00")
    end_time: Mapped[str] = mapped_column(String(5), default="23:59")
    device_timezone_id: Mapped[int] = mapped_column(Integer, default=1)
    normal_open: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(String(128))

    device: Mapped[Device] = relationship(back_populates="schedules")


class SyncRun(Base):
    """Audit record for a sync / polling job execution."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    items_processed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class User(TimestampMixin, Base):
    """A dashboard login.

    Intentionally minimal — no email, no self-registration, no password-reset
    mail: this runs on one LAN host, and an admin can reset any password from the
    Pengguna tab. ``role`` is the whole permission model (see `ROLES`).
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    #: PBKDF2-HMAC-SHA256, produced by `auth_service.hash_password`. A plain
    #: password is never stored, logged or returned by the API.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=ROLE_HR)
    full_name: Mapped[str | None] = mapped_column(String(128))
    #: Deactivating is the safe way to remove someone: it keeps the account (and
    #: its audit trail) but refuses the login and kills every live session.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Set for the accounts seeded on first start. It only drives a warning in the
    #: dashboard — it never blocks a login, because a blocked-out admin with no
    #: other account and no way in is worse than a default password on a LAN box.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    """One logged-in browser/script session.

    Sessions are rows, not signed cookies: logging out — or deactivating a user —
    really ends the session on the server instead of waiting for a token to expire.
    Only the SHA-256 of the cookie value is stored, so a database dump cannot be
    replayed as a login.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: SHA-256 of the cookie value. UNIQUE is enough of an index for the lookup, and
    #: a plain constraint keeps the migration and `create_all` identical.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")
