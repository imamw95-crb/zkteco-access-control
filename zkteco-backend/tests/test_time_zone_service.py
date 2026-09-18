"""Tests for access-control time zones ("jam akses")."""

from __future__ import annotations

import pytest

from app.schemas import AccessGroupCreate, AccessGroupDoorCreate, TimeZoneCreate, TimeZoneUpdate
from app.services.access_group_service import AccessGroupService
from app.services.time_zone_service import (
    DAYS,
    DuplicateTimeZoneError,
    TimeZoneInUseError,
    TimeZoneNotFoundError,
    TimeZoneService,
)


def _slot(day: int, slot: int, start: str, end: str) -> dict:
    return {"day_of_week": day, "slot": slot, "start_time": start, "end_time": end}


@pytest.fixture
def tz(db):
    return TimeZoneService(db)


@pytest.fixture
def group(db):
    return AccessGroupService(db).create(AccessGroupCreate(name="ICU"))


def test_24_hour_preset_has_every_day_open(tz):
    zone = tz.ensure_24_hour(1)

    assert zone.name == "24 Jam"
    assert zone.device_timezone_id == 1
    assert zone.is_24_hour is True
    assert len(zone.slots) == 7 * 3

    for day in range(7):
        active = zone.active_slots(day)
        assert len(active) == 1, f"{DAYS[day]} should have exactly one active segment"
        assert (active[0].start_time, active[0].end_time) == ("00:00", "23:59")


def test_empty_slots_are_not_active(tz):
    zone = tz.ensure_24_hour(1)

    assert zone.active_slots(0)[0].slot == 1
    assert all(not s.is_empty for s in zone.active_slots(0))
    assert all(s.is_empty for s in zone.slots if s.slot != 1)


def test_ensure_24_hour_is_idempotent(tz):
    first = tz.ensure_24_hour(1)
    second = tz.ensure_24_hour(1)

    assert first.id == second.id
    assert len(tz.list()) == 1


def test_device_value_encoding_matches_the_panel(tz):
    """The firmware stores a segment end as HHMM (23:59 -> 2359)."""
    zone = tz.ensure_24_hour(1)
    slot = zone.active_slots(0)[0]

    assert slot.from_device_value == 0  # 00:00
    assert slot.to_device_value == 2359  # 23:59


def test_create_custom_zone(tz):
    zone = tz.create(
        TimeZoneCreate(
            name="Jam Kerja",
            device_timezone_id=2,
            description="Senin-Jumat 07:00-17:00",
            slots=[
                _slot(day, 1, "07:00", "17:00")
                for day in range(1, 6)  # Mon..Fri, device order 0=Sunday
            ],
        )
    )

    assert zone.id is not None
    assert zone.is_24_hour is False
    assert [s.end_time for s in zone.active_slots(1)] == ["17:00"]
    assert zone.active_slots(0) == []  # Sunday closed


def test_duplicate_name_rejected(tz):
    tz.create(TimeZoneCreate(name="A", device_timezone_id=1))
    with pytest.raises(DuplicateTimeZoneError):
        tz.create(TimeZoneCreate(name="A", device_timezone_id=2))


def test_duplicate_device_slot_rejected(tz):
    tz.create(TimeZoneCreate(name="A", device_timezone_id=1))
    with pytest.raises(DuplicateTimeZoneError):
        tz.create(TimeZoneCreate(name="B", device_timezone_id=1))


def test_duplicate_day_slot_rejected(tz):
    with pytest.raises(DuplicateTimeZoneError):
        tz.create(
            TimeZoneCreate(
                name="A",
                device_timezone_id=1,
                slots=[_slot(0, 1, "00:00", "12:00"), _slot(0, 1, "13:00", "17:00")],
            )
        )


def test_get_missing_raises(tz):
    with pytest.raises(TimeZoneNotFoundError):
        tz.get(999)


def test_update_replaces_slots(tz):
    zone = tz.ensure_24_hour(1)

    updated = tz.update(
        zone.id,
        TimeZoneUpdate(
            name="24 Jam (ubah)",
            is_24_hour=False,
            slots=[_slot(0, 1, "06:00", "18:00")],
        ),
    )

    assert updated.name == "24 Jam (ubah)"
    assert updated.is_24_hour is False
    assert len(updated.slots) == 1
    assert updated.active_slots(0)[0].start_time == "06:00"


def test_update_without_slots_keeps_them(tz):
    zone = tz.ensure_24_hour(1)

    tz.update(zone.id, TimeZoneUpdate(description="catatan"))

    assert len(tz.get(zone.id).slots) == 21


def test_lookup_by_device_id(tz):
    zone = tz.ensure_24_hour(1)

    assert tz.get_by_device_id(1).id == zone.id
    assert tz.get_by_device_id(99) is None
    assert tz.name_for_device_id(1) == "24 Jam"
    assert tz.name_for_device_id(99) is None
    assert tz.name_for_device_id(None) is None


# -- linking to access levels ----------------------------------------------
def test_group_resolves_its_time_zone(tz, group):
    tz.ensure_24_hour(1)

    zone = tz.for_group(group)

    assert zone is not None
    assert zone.name == "24 Jam"


def test_for_group_returns_none_when_undefined(tz, group):
    assert tz.for_group(group) is None


def test_set_group_time_zone(tz, group):
    zone = tz.create(TimeZoneCreate(name="Jam Kerja", device_timezone_id=4))

    updated = tz.set_group_time_zone(group.id, zone.id)

    assert updated.device_timezone_id == 4
    assert tz.for_group(updated).name == "Jam Kerja"


def test_set_group_time_zone_missing_group(tz):
    zone = tz.create(TimeZoneCreate(name="A", device_timezone_id=1))
    with pytest.raises(TimeZoneNotFoundError):
        tz.set_group_time_zone(999, zone.id)


def test_delete_zone_in_use_is_refused(tz, db, group, make_device):
    """Deleting a zone an access level still points at must not silently orphan it."""
    zone = tz.ensure_24_hour(1)
    AccessGroupService(db).add_door(
        group.id, AccessGroupDoorCreate(device_id=make_device(ip="10.100.1.11").id)
    )

    with pytest.raises(TimeZoneInUseError):
        tz.delete(zone.id)


def test_delete_unused_zone(tz):
    zone = tz.create(TimeZoneCreate(name="Cadangan", device_timezone_id=9))

    tz.delete(zone.id)

    assert tz.list() == []
