"""Tests for access level management (ZKAccess "access levels")."""

from __future__ import annotations

import pytest

from app.schemas import AccessGroupCreate, AccessGroupDoorCreate, AccessGroupUpdate
from app.services.access_group_service import (
    AccessGroupNotFoundError,
    AccessGroupService,
    DuplicateAccessGroupError,
)


def test_create_and_list(db):
    service = AccessGroupService(db)
    service.create(AccessGroupCreate(name="ICU", description="Akses ruang ICU"))

    groups = service.list()
    assert [g.name for g in groups] == ["ICU"]
    assert groups[0].door_count == 0
    assert groups[0].member_count == 0


def test_duplicate_name_rejected(db):
    service = AccessGroupService(db)
    service.create(AccessGroupCreate(name="ICU"))
    with pytest.raises(DuplicateAccessGroupError):
        service.create(AccessGroupCreate(name="ICU"))


def test_get_missing_raises(db):
    with pytest.raises(AccessGroupNotFoundError):
        AccessGroupService(db).get(999)


def test_update_and_delete(db):
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="ICU"))

    updated = service.update(group.id, AccessGroupUpdate(name="ICU / NICU"))
    assert updated.name == "ICU / NICU"

    service.delete(group.id)
    assert service.list() == []


# -- doors -----------------------------------------------------------------
def test_add_door_and_count(db, make_device):
    device = make_device(name="ICU", ip="10.100.1.11")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="ICU"))

    link = service.add_door(group.id, AccessGroupDoorCreate(device_id=device.id, door_number=1))

    assert link.device_id == device.id
    assert service.get(group.id).door_count == 1
    assert link.device_name == "ICU"
    assert link.device_ip == "10.100.1.11"


def test_add_same_door_twice_is_idempotent(db, make_device):
    device = make_device(ip="10.100.1.11")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="ICU"))

    first = service.add_door(group.id, AccessGroupDoorCreate(device_id=device.id, door_number=1))
    second = service.add_door(group.id, AccessGroupDoorCreate(device_id=device.id, door_number=1))

    assert first.id == second.id
    assert service.get(group.id).door_count == 1


def test_group_can_span_many_devices(db, make_device):
    """Legacy TEKNISI/IT covers 22 doors across 20 devices."""
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="TEKNISI/IT"))
    for i in range(1, 6):
        device = make_device(name=f"D{i}", ip=f"10.100.1.{i}")
        service.add_door(group.id, AccessGroupDoorCreate(device_id=device.id, door_number=1))

    assert service.get(group.id).door_count == 5


def test_add_door_unknown_device_raises(db):
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="ICU"))
    with pytest.raises(AccessGroupNotFoundError):
        service.add_door(group.id, AccessGroupDoorCreate(device_id=999, door_number=1))


def test_remove_door(db, make_device):
    device = make_device(ip="10.100.1.11")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="ICU"))
    link = service.add_door(group.id, AccessGroupDoorCreate(device_id=device.id))

    service.remove_door(group.id, link.id)

    assert service.get(group.id).door_count == 0


def test_remove_door_wrong_group_raises(db, make_device):
    device = make_device(ip="10.100.1.11")
    service = AccessGroupService(db)
    first = service.create(AccessGroupCreate(name="A"))
    second = service.create(AccessGroupCreate(name="B"))
    link = service.add_door(first.id, AccessGroupDoorCreate(device_id=device.id))

    with pytest.raises(AccessGroupNotFoundError):
        service.remove_door(second.id, link.id)


def test_refresh_door_numbers_cache(db, make_device):
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="A"))
    for number in (1, 2):
        device = make_device(name=f"D{number}", ip=f"10.100.1.{number}")
        service.add_door(group.id, AccessGroupDoorCreate(device_id=device.id, door_number=number))

    refreshed = service.refresh_door_numbers_cache(group.id)

    assert refreshed.door_numbers == "1,2"


# -- members ---------------------------------------------------------------
def test_add_member_by_employee_id(db, make_person):
    make_person(employee_id="202307056", name="Imam")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="TEKNISI/IT"))

    result = service.add_members(group.id, employee_ids=["202307056"])

    assert result["added"] == 1
    assert result["skipped"] == 0
    assert service.get(group.id).member_count == 1


def test_add_members_bulk_and_skip_duplicates(db, make_person):
    for badge in ("1", "2", "3"):
        make_person(employee_id=badge, name=f"Orang {badge}")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="AKSES UMUM"))

    first = service.add_members(group.id, employee_ids=["1", "2", "3"])
    second = service.add_members(group.id, employee_ids=["1", "2", "3", "tidak-ada"])

    assert first["added"] == 3
    assert second["added"] == 0
    assert second["skipped"] == 4  # 3 duplicates + 1 unknown badge


def test_add_member_by_personnel_id(db, make_person):
    person = make_person(employee_id="1", name="A")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="ICU"))

    result = service.add_members(group.id, personnel_ids=[person.id])

    assert result["added"] == 1


def test_person_can_hold_many_groups(db, make_person):
    person = make_person(employee_id="1", name="A")
    service = AccessGroupService(db)
    groups = [service.create(AccessGroupCreate(name=f"G{i}")) for i in range(4)]

    for group in groups:
        service.add_members(group.id, personnel_ids=[person.id])

    assert [g.member_count for g in service.list()] == [1, 1, 1, 1]


def test_remove_member(db, make_person):
    person = make_person(employee_id="1", name="A")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="ICU"))
    service.add_members(group.id, personnel_ids=[person.id])

    service.remove_member(group.id, person.id)

    assert service.get(group.id).member_count == 0


def test_remove_member_not_in_group_raises(db, make_person):
    person = make_person(employee_id="1", name="A")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="ICU"))

    with pytest.raises(AccessGroupNotFoundError):
        service.remove_member(group.id, person.id)


def test_members_are_sorted_by_employee_id(db, make_person):
    for badge in ("3", "1", "2"):
        make_person(employee_id=badge, name=f"Orang {badge}")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="A"))
    service.add_members(group.id, employee_ids=["3", "1", "2"])

    assert [p.employee_id for p in service.members(group.id)] == ["1", "2", "3"]


def test_deleting_group_removes_links(db, make_device, make_person):
    from app.models import AccessGroupDoor, PersonnelAccessGroup

    device = make_device(ip="10.100.1.11")
    person = make_person(employee_id="1", name="A")
    service = AccessGroupService(db)
    group = service.create(AccessGroupCreate(name="ICU"))
    service.add_door(group.id, AccessGroupDoorCreate(device_id=device.id))
    service.add_members(group.id, personnel_ids=[person.id])

    service.delete(group.id)

    assert db.query(AccessGroupDoor).count() == 0
    assert db.query(PersonnelAccessGroup).count() == 0
