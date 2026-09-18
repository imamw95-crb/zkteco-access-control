"""Tests for personnel handling and device read-back."""

from __future__ import annotations

import pytest

from app.schemas import PersonnelCreate
from app.services.access_group_service import AccessGroupNotFoundError
from app.services.personnel_service import (
    DuplicatePersonnelError,
    PersonnelNotFoundError,
    PersonnelService,
)
from app.services.push_agent import PushAgentNotConfigured


def _seed_device_users(fake_clients, ip: str, count: int = 3) -> None:
    fake_clients.users[ip] = [
        {
            "UID": str(1000 + i),
            "CardNo": str(5000 + i),
            "Pin": str(1000 + i),
            "Name": f"User {i}",
            "Group": "1",
        }
        for i in range(1, count + 1)
    ]


def test_personnel_crud(db):
    service = PersonnelService(db)
    person = service.create(PersonnelCreate(employee_id="1001", name="Budi", card_number="5001"))

    assert service.count() == 1
    assert service.get(person.id).name == "Budi"

    from app.schemas import PersonnelUpdate

    updated = service.update(person.id, PersonnelUpdate(name="Budi Santoso"))
    assert updated.name == "Budi Santoso"

    service.delete(person.id)
    assert service.count() == 0


def test_duplicate_employee_id_rejected(db):
    service = PersonnelService(db)
    service.create(PersonnelCreate(employee_id="1001", name="Budi"))
    with pytest.raises(DuplicatePersonnelError):
        service.create(PersonnelCreate(employee_id="1001", name="Ani"))


def test_duplicate_card_number_rejected(db):
    service = PersonnelService(db)
    service.create(PersonnelCreate(employee_id="1001", name="Budi", card_number="5001"))
    with pytest.raises(DuplicatePersonnelError):
        service.create(PersonnelCreate(employee_id="1002", name="Ani", card_number="5001"))


def test_duplicate_error_names_the_field_and_its_holder(db):
    """The operator has to be told *which* value clashed and who holds it.

    A raw IntegrityError only says "UNIQUE constraint failed", which leaves
    nobody able to tell whether the badge or the card was the problem.
    """
    service = PersonnelService(db)
    service.create(PersonnelCreate(employee_id="1001", name="Budi", card_number="5001"))

    with pytest.raises(DuplicatePersonnelError) as card_clash:
        service.create(PersonnelCreate(employee_id="1002", name="Ani", card_number="5001"))
    assert "No. kartu 5001" in str(card_clash.value)
    assert "Budi" in str(card_clash.value)

    with pytest.raises(DuplicatePersonnelError) as badge_clash:
        service.create(PersonnelCreate(employee_id="1001", name="Ani"))
    assert "Badge/PIN 1001" in str(badge_clash.value)
    assert "Budi" in str(badge_clash.value)


def test_blank_card_is_stored_as_null_so_people_without_cards_do_not_clash(db):
    """`card_number` is nullable but UNIQUE.

    An empty string is a *value*, so two people saved with "" would collide even
    though neither actually has a card. Blanks must become NULL.
    """
    service = PersonnelService(db)
    first = service.create(PersonnelCreate(employee_id="1001", name="Budi", card_number=""))
    second = service.create(PersonnelCreate(employee_id="1002", name="Ani", card_number="   "))

    assert service.get(first.id).card_number is None
    assert service.get(second.id).card_number is None
    assert service.count() == 2


def test_badge_with_surrounding_spaces_is_treated_as_the_same_badge(db):
    service = PersonnelService(db)
    service.create(PersonnelCreate(employee_id="1001", name="Budi"))

    with pytest.raises(DuplicatePersonnelError):
        service.create(PersonnelCreate(employee_id="  1001  ", name="Ani"))


def test_person_may_keep_their_own_card_when_updated(db):
    """Re-saving a person's existing card must not trip the duplicate guard."""
    from app.schemas import PersonnelUpdate

    service = PersonnelService(db)
    person = service.create(PersonnelCreate(employee_id="1001", name="Budi", card_number="5001"))

    updated = service.update(person.id, PersonnelUpdate(card_number="5001", name="Budi S"))
    assert updated.card_number == "5001"
    assert updated.name == "Budi S"


def test_update_refuses_a_card_held_by_somebody_else(db):
    from app.schemas import PersonnelUpdate

    service = PersonnelService(db)
    service.create(PersonnelCreate(employee_id="1001", name="Budi", card_number="5001"))
    ani = service.create(PersonnelCreate(employee_id="1002", name="Ani"))

    with pytest.raises(DuplicatePersonnelError) as clash:
        service.update(ani.id, PersonnelUpdate(card_number="5001"))
    assert "No. kartu 5001" in str(clash.value)
    assert "Budi" in str(clash.value)


def test_clearing_a_card_releases_it_for_somebody_else(db):
    """Detaching a card (send null) must free it for re-issue."""
    from app.schemas import PersonnelUpdate

    service = PersonnelService(db)
    budi = service.create(PersonnelCreate(employee_id="1001", name="Budi", card_number="5001"))
    service.update(budi.id, PersonnelUpdate(card_number=None))

    assert service.get(budi.id).card_number is None
    reused = service.create(PersonnelCreate(employee_id="1002", name="Ani", card_number="5001"))
    assert service.get(reused.id).card_number == "5001"


def test_person_can_be_created_with_several_access_levels_at_once(db):
    """`access_group_ids` must attach every level in one create.

    A person typically holds several levels (the migrated data reaches 11), so
    the add-personnel form has to accept a set rather than one id.
    """
    from app.models import AccessGroup

    groups = [AccessGroup(name=n) for n in ("ICU", "ISOLASI", "RANAP OK")]
    db.add_all(groups)
    db.commit()

    service = PersonnelService(db)
    person = service.create(
        PersonnelCreate(employee_id="1001", name="Budi", access_group_ids=[g.id for g in groups])
    )

    assert service.get(person.id).access_group_names == ["ICU", "ISOLASI", "RANAP OK"]


def test_unknown_access_level_aborts_a_multi_level_create(db):
    """A bad id anywhere in the list must not create the person at all."""
    from app.models import AccessGroup

    good = AccessGroup(name="ICU")
    db.add(good)
    db.commit()

    service = PersonnelService(db)
    with pytest.raises(AccessGroupNotFoundError):
        service.create(
            PersonnelCreate(employee_id="1001", name="Budi", access_group_ids=[good.id, 999])
        )
    assert service.count() == 0


def test_read_exposes_group_ids_so_an_edit_form_can_tick_the_right_boxes(db):
    """`access_group_ids` must mirror `access_group_names`.

    The edit screen needs ids; matching on names would break as soon as a level
    is renamed.
    """
    from app.models import AccessGroup

    groups = [AccessGroup(name=n) for n in ("ICU", "ISOLASI")]
    db.add_all(groups)
    db.commit()

    service = PersonnelService(db)
    person = service.create(
        PersonnelCreate(employee_id="1001", name="Budi", access_group_ids=[g.id for g in groups])
    )

    fetched = service.get(person.id)
    assert fetched.access_group_ids == sorted(g.id for g in groups)
    assert len(fetched.access_group_names) == len(fetched.access_group_ids)


def test_update_can_change_the_badge(db):
    from app.schemas import PersonnelUpdate

    service = PersonnelService(db)
    person = service.create(PersonnelCreate(employee_id="1001", name="Budi"))

    updated = service.update(person.id, PersonnelUpdate(employee_id="1002"))

    assert updated.employee_id == "1002"


def test_update_refuses_a_badge_held_by_somebody_else(db):
    from app.schemas import PersonnelUpdate

    service = PersonnelService(db)
    service.create(PersonnelCreate(employee_id="1001", name="Budi"))
    ani = service.create(PersonnelCreate(employee_id="1002", name="Ani"))

    with pytest.raises(DuplicatePersonnelError) as clash:
        service.update(ani.id, PersonnelUpdate(employee_id="1001"))
    assert "Badge/PIN 1001" in str(clash.value)
    assert "Budi" in str(clash.value)
    assert service.get(ani.id).employee_id == "1002"


def test_update_keeps_the_badge_when_none_is_sent(db):
    """A null badge means "leave it alone" — not "erase it" (the column is NOT NULL)."""
    from app.schemas import PersonnelUpdate

    service = PersonnelService(db)
    person = service.create(PersonnelCreate(employee_id="1001", name="Budi"))

    updated = service.update(person.id, PersonnelUpdate(employee_id=None, name="Budi S"))

    assert updated.employee_id == "1001"
    assert updated.name == "Budi S"


def test_update_replaces_the_access_level_set(db):
    """Editing shows the complete set, so the saved set must match it exactly."""
    from app.models import AccessGroup
    from app.schemas import PersonnelUpdate

    groups = [AccessGroup(name=n) for n in ("ICU", "ISOLASI", "RANAP OK")]
    db.add_all(groups)
    db.commit()
    icu, isolasi, ranap = groups

    service = PersonnelService(db)
    person = service.create(
        PersonnelCreate(employee_id="1001", name="Budi", access_group_ids=[icu.id, isolasi.id])
    )

    updated = service.update(person.id, PersonnelUpdate(access_group_ids=[isolasi.id, ranap.id]))
    assert updated.access_group_names == ["ISOLASI", "RANAP OK"]

    cleared = service.update(person.id, PersonnelUpdate(access_group_ids=[]))
    assert cleared.access_group_names == []


def test_explicit_null_department_without_clear_flag_leaves_it_alone(db):
    """The edit form always sends `department_id`; null alone must not detach.

    Detaching is an explicit intent, signalled by `clear_department`, so a form
    that always includes the key cannot silently wipe the department.
    """
    from app.models import Department
    from app.schemas import PersonnelUpdate

    dept = Department(name="ICU")
    db.add(dept)
    db.commit()

    service = PersonnelService(db)
    person = service.create(PersonnelCreate(employee_id="1001", name="Budi", department_id=dept.id))
    assert service.get(person.id).department == "ICU"

    kept = service.update(person.id, PersonnelUpdate(name="Budi S", department_id=None))
    assert kept.department == "ICU"

    detached = service.update(person.id, PersonnelUpdate(clear_department=True))
    assert detached.department is None


def test_search_by_name_and_id(db):
    service = PersonnelService(db)
    service.create(PersonnelCreate(employee_id="1001", name="Budi Santoso"))
    service.create(PersonnelCreate(employee_id="2002", name="Ani Yulia"))

    assert [p.employee_id for p in service.list(q="budi")] == ["1001"]
    assert [p.employee_id for p in service.list(q="2002")] == ["2002"]
    assert len(service.list(q="i")) == 2


def test_get_missing_raises(db):
    with pytest.raises(PersonnelNotFoundError):
        PersonnelService(db).get(999)


def test_count_on_device_reads_actual_users(db, make_device, fake_clients):
    device = make_device(ip="10.100.1.14")
    _seed_device_users(fake_clients, "10.100.1.14", count=42)

    assert PersonnelService(db).count_on_device(device.id) == 42


def test_pull_from_device_returns_records_and_updates_cache(db, make_device, fake_clients):
    device = make_device(ip="10.100.1.14")
    _seed_device_users(fake_clients, "10.100.1.14", count=2)

    records = PersonnelService(db).pull_from_device(device.id)

    assert len(records) == 2
    assert records[0]["UID"] == "1001"
    assert records[0]["CardNo"] == "5001"

    db.refresh(device)
    assert device.personnel_count == 2


def test_import_from_device_creates_personnel(db, make_device, fake_clients):
    device = make_device(ip="10.100.1.14")
    _seed_device_users(fake_clients, "10.100.1.14", count=3)

    result = PersonnelService(db).import_from_device(device.id)

    assert result["created"] == 3
    assert result["device_records"] == 3
    service = PersonnelService(db)
    assert service.count() == 3
    assert service.list()[0].name.startswith("User")


def test_import_from_device_is_idempotent(db, make_device, fake_clients):
    device = make_device(ip="10.100.1.14")
    _seed_device_users(fake_clients, "10.100.1.14", count=3)
    service = PersonnelService(db)

    service.import_from_device(device.id)
    second = service.import_from_device(device.id)

    assert second["created"] == 0
    assert second["skipped"] == 3
    assert service.count() == 3


def test_import_with_overwrite_updates_existing(db, make_device, fake_clients):
    device = make_device(ip="10.100.1.14")
    _seed_device_users(fake_clients, "10.100.1.14", count=1)
    service = PersonnelService(db)
    service.create(PersonnelCreate(employee_id="1001", name="Nama Lama"))

    result = service.import_from_device(device.id, overwrite=True)

    assert result["updated"] == 1
    assert service.list()[0].name == "User 1"


def test_push_to_device_without_an_agent_explains_the_setup(db, make_device):
    """Pushing is delegated to a Windows agent; unconfigured is an error, not silence."""
    device = make_device(ip="10.100.1.14")

    with pytest.raises(PushAgentNotConfigured) as exc:
        PersonnelService(db).push_to_device(device.id)

    assert "PUSH_AGENT_URL" in str(exc.value)


# -- pushing one person to every panel their levels reach -----------------
def _level_covering(db, level_name: str, devices: list, door_number: int = 1):
    """An access level with a door on each of the given devices."""
    from app.models import AccessGroup, AccessGroupDoor

    level = AccessGroup(name=level_name)
    db.add(level)
    db.commit()
    for device in devices:
        db.add(
            AccessGroupDoor(access_group_id=level.id, device_id=device.id, door_number=door_number)
        )
    db.commit()
    return level


def _join(db, person, *levels):
    from app.models import PersonnelAccessGroup

    for level in levels:
        db.add(PersonnelAccessGroup(personnel_id=person.id, access_group_id=level.id))
    db.commit()


def test_devices_for_personnel_covers_every_panel_of_their_levels(db, make_device, make_person):
    """The regression this exists for: one level, many panels.

    A level covering twenty panels was effectively read as covering only the one
    panel the operator happened to be looking at, so a new person landed on a
    single door while the other nineteen stayed stale. Every panel the level
    touches has to come back.
    """
    server = make_device(name="RUANGAN SERVER", ip="10.100.1.12")
    igd = make_device(name="IGD Kiri", ip="10.100.1.14")
    gizi = make_device(name="GIZI BELAKANG", ip="10.100.1.9")
    elsewhere = make_device(name="ISOLASI", ip="10.100.1.20")

    it = _level_covering(db, "TEKNISI/IT", [server, igd])
    _level_covering(db, "GIZI", [gizi])
    person = make_person(employee_id="2150141426", name="tes2150141426")
    _join(db, person, it)

    devices = PersonnelService(db).devices_for_personnel([person.id])

    # Sorted by name, and the panels of other levels are not dragged in.
    assert [d.name for d in devices] == ["IGD Kiri", "RUANGAN SERVER"]
    assert elsewhere not in devices


def test_devices_for_personnel_unions_several_levels(db, make_device, make_person):
    """Two levels means the union of their panels, and a shared panel appears once."""
    first = make_device(name="ICU", ip="10.100.1.11")
    second = make_device(name="ISOLASI", ip="10.100.1.20")

    one = _level_covering(db, "ICU", [first, second])
    two = _level_covering(db, "ISOLASI", [second])
    person = make_person()
    _join(db, person, one, two)

    devices = PersonnelService(db).devices_for_personnel([person.id])

    assert [d.name for d in devices] == ["ICU", "ISOLASI"]


def test_devices_for_personnel_is_empty_without_access_levels(db, make_device, make_person):
    """No level means no door to reach — not every panel in the estate."""
    make_device()
    person = make_person()

    assert PersonnelService(db).devices_for_personnel([person.id]) == []


def test_devices_for_personnel_is_empty_for_no_ids(db):
    assert PersonnelService(db).devices_for_personnel([]) == []


def test_push_to_all_devices_without_an_agent_explains_the_setup(db, make_device, make_person):
    device = make_device()
    level = _level_covering(db, "TEKNISI/IT", [device])
    person = make_person()
    _join(db, person, level)

    with pytest.raises(PushAgentNotConfigured) as exc:
        PersonnelService(db).push_to_all_devices([person.id])

    assert "PUSH_AGENT_URL" in str(exc.value)


def test_push_to_all_devices_with_no_panels_needs_no_agent(db, make_person):
    """Somebody with no access level reaches no door; that is not an agent problem.

    Raising 503 here would be actively misleading — it would tell the operator to
    go and configure a push agent when the real answer is "this person has not
    been given an access level yet".
    """
    person = make_person()

    result = PersonnelService(db).push_to_all_devices([person.id])

    assert result["device_count"] == 0
    assert result["results"] == []
    assert result["failed"] == 0


def test_iter_push_to_all_devices_reports_each_panel_as_it_finishes(db, make_device, make_person):
    """The sweep is watched panel by panel, not collected up and handed over at the end.

    A whole sweep takes long enough that one final answer cannot tell progress from
    a hang, and leaves the operator unable to say which panels are already written.
    The summary must still come last, so a sweep that went badly can say how far it
    got.
    """
    icu = make_device(name="ICU", ip="10.100.1.11")
    make_device(name="ISOLASI", ip="10.100.1.20")
    level = _level_covering(db, "ICU", [icu])
    person = make_person()
    _join(db, person, level)

    events = list(PersonnelService(db).iter_push_to_all_devices([person.id], dry_run=True))

    assert [event["type"] for event in events] == ["panel", "panel", "summary"]
    panels = events[:-1]
    assert [panel["device_name"] for panel in panels] == ["ICU", "ISOLASI"]
    assert [panel["covered"] for panel in panels] == [True, False]
    assert events[-1]["device_count"] == 2
    assert events[-1]["covered_count"] == 1


def test_iter_push_to_all_devices_can_be_narrowed_to_named_panels(db, make_device, make_person):
    """Granting a level to somebody concerns that level's panels, not the estate.

    The wide walk exists to hunt stale grants everywhere; reporting the other
    panels as "tidak memuat orang ini" just buries the ones the operator asked
    about. `covered_count` has to describe the panels that were actually visited,
    or a narrowed sweep claims to cover more panels than it looked at.
    """
    icu = make_device(name="ICU", ip="10.100.1.11")
    gizi = make_device(name="GIZI", ip="10.100.1.9")
    make_device(name="ISOLASI", ip="10.100.1.20")
    level = _level_covering(db, "ICU", [icu, gizi])
    person = make_person()
    _join(db, person, level)

    events = list(
        PersonnelService(db).iter_push_to_all_devices(
            [person.id], dry_run=True, device_ids=[gizi.id]
        )
    )

    panels = [event for event in events if event["type"] == "panel"]
    assert [panel["device_name"] for panel in panels] == ["GIZI"]
    summary = events[-1]
    assert summary["device_count"] == 1
    assert summary["covered_count"] == 1


# -- many-to-many access groups (mirrors the legacy ZKAccess data) ---------
def test_access_group_names_is_empty_without_memberships(db):
    service = PersonnelService(db)
    person = service.create(PersonnelCreate(employee_id="1001", name="Budi"))
    assert service.get(person.id).access_group_names == []


def test_person_can_hold_several_access_groups(db):
    """Legacy data has people in up to 11 groups; a single FK cannot hold that."""
    from app.models import AccessGroup, PersonnelAccessGroup

    incoming = AccessGroup(name="AKSES UMUM KARYAWAN")
    icu = AccessGroup(name="ICU")
    isolasi = AccessGroup(name="ISOLASI")
    db.add_all([incoming, icu, isolasi])
    db.commit()

    service = PersonnelService(db)
    person = service.create(PersonnelCreate(employee_id="202307056", name="Imam"))
    for group in (incoming, icu, isolasi):
        db.add(PersonnelAccessGroup(personnel_id=person.id, access_group_id=group.id))
    db.commit()

    names = service.get(person.id).access_group_names
    assert names == ["AKSES UMUM KARYAWAN", "ICU", "ISOLASI"]


def test_access_group_names_survive_list_query(db):
    from app.models import AccessGroup, PersonnelAccessGroup

    group = AccessGroup(name="TEKNISI/IT")
    db.add(group)
    db.commit()
    service = PersonnelService(db)
    person = service.create(PersonnelCreate(employee_id="1", name="A"))
    db.add(PersonnelAccessGroup(personnel_id=person.id, access_group_id=group.id))
    db.commit()
    db.expunge_all()  # force a real re-query rather than identity-map hits

    assert service.list()[0].access_group_names == ["TEKNISI/IT"]
