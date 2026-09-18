"""Tests for the department master: CRUD, hierarchy, and linking to personnel."""

from __future__ import annotations

import pytest

from app.schemas import DepartmentCreate, DepartmentUpdate, PersonnelCreate, PersonnelUpdate
from app.services.department_service import (
    DepartmentCycleError,
    DepartmentInUseError,
    DepartmentNotFoundError,
    DepartmentService,
    DuplicateDepartmentError,
)
from app.services.personnel_service import PersonnelService


def test_create_and_list(db):
    service = DepartmentService(db)
    service.create(DepartmentCreate(name="Bidang Keperawatan"))

    assert [d.name for d in service.list()] == ["Bidang Keperawatan"]


def test_duplicate_name_rejected(db):
    service = DepartmentService(db)
    service.create(DepartmentCreate(name="ICU"))
    with pytest.raises(DuplicateDepartmentError):
        service.create(DepartmentCreate(name="ICU"))


def test_get_missing_raises(db):
    with pytest.raises(DepartmentNotFoundError):
        DepartmentService(db).get(999)


def test_hierarchy_and_full_path(db):
    service = DepartmentService(db)
    root = service.create(DepartmentCreate(name="RSMP PATROL"))
    child = service.create(DepartmentCreate(name="Bidang Keperawatan", parent_id=root.id))
    grandchild = service.create(DepartmentCreate(name="ICU", parent_id=child.id))

    assert grandchild.full_path == "RSMP PATROL / Bidang Keperawatan / ICU"
    assert grandchild.parent.name == "Bidang Keperawatan"
    assert service.get(root.id).full_path == "RSMP PATROL"


def test_create_with_unknown_parent_raises(db):
    with pytest.raises(DepartmentNotFoundError):
        DepartmentService(db).create(DepartmentCreate(name="A", parent_id=999))


def test_update_and_clear_parent(db):
    service = DepartmentService(db)
    root = service.create(DepartmentCreate(name="Root"))
    child = service.create(DepartmentCreate(name="Child", parent_id=root.id))

    moved = service.update(child.id, DepartmentUpdate(clear_parent=True))
    assert moved.parent_id is None

    back = service.update(child.id, DepartmentUpdate(parent_id=root.id))
    assert back.parent_id == root.id


def test_department_cannot_become_its_own_parent(db):
    service = DepartmentService(db)
    department = service.create(DepartmentCreate(name="A"))

    with pytest.raises(DepartmentCycleError):
        service.update(department.id, DepartmentUpdate(parent_id=department.id))


def test_department_cannot_move_under_its_own_descendant(db):
    service = DepartmentService(db)
    root = service.create(DepartmentCreate(name="Root"))
    child = service.create(DepartmentCreate(name="Child", parent_id=root.id))

    with pytest.raises(DepartmentCycleError):
        service.update(root.id, DepartmentUpdate(parent_id=child.id))


def test_delete_with_personnel_is_refused(db):
    service = DepartmentService(db)
    department = service.create(DepartmentCreate(name="ICU"))
    PersonnelService(db).create(
        PersonnelCreate(employee_id="1", name="A", department_id=department.id)
    )

    with pytest.raises(DepartmentInUseError):
        service.delete(department.id)


def test_delete_reparents_children(db):
    service = DepartmentService(db)
    root = service.create(DepartmentCreate(name="Root"))
    middle = service.create(DepartmentCreate(name="Middle", parent_id=root.id))
    leaf = service.create(DepartmentCreate(name="Leaf", parent_id=middle.id))

    service.delete(middle.id)

    assert service.get(leaf.id).parent_id == root.id


def test_delete_unused_department(db):
    service = DepartmentService(db)
    department = service.create(DepartmentCreate(name="Kosong"))

    service.delete(department.id)

    assert service.list() == []


def test_personnel_counts(db):
    service = DepartmentService(db)
    icu = service.create(DepartmentCreate(name="ICU"))
    umum = service.create(DepartmentCreate(name="UMUM"))
    personnel = PersonnelService(db)
    personnel.create(PersonnelCreate(employee_id="1", name="A", department_id=icu.id))
    personnel.create(PersonnelCreate(employee_id="2", name="B", department_id=icu.id))
    personnel.create(PersonnelCreate(employee_id="3", name="C", department_id=umum.id))

    counts = service.personnel_counts()

    assert counts[icu.id] == 2
    assert counts[umum.id] == 1
    assert service.count_personnel(icu.id) == 2


# -- linking personnel -----------------------------------------------------
def test_personnel_linked_by_department_id(db):
    department = DepartmentService(db).create(DepartmentCreate(name="ICU"))

    person = PersonnelService(db).create(
        PersonnelCreate(employee_id="1", name="A", department_id=department.id)
    )

    assert person.department_id == department.id
    assert person.department == "ICU"


def test_personnel_department_by_name_is_resolved(db):
    person = PersonnelService(db).create(
        PersonnelCreate(employee_id="1", name="A", department="Bidang Keperawatan")
    )

    assert person.department == "Bidang Keperawatan"
    assert person.department_id is not None


def test_unknown_department_name_is_created_once(db):
    service = PersonnelService(db)
    service.create(PersonnelCreate(employee_id="1", name="A", department="TEKNISI"))
    service.create(PersonnelCreate(employee_id="2", name="B", department="TEKNISI"))

    departments = DepartmentService(db).list()
    assert [d.name for d in departments] == ["TEKNISI"]
    assert DepartmentService(db).count_personnel(departments[0].id) == 2


def test_unknown_department_id_raises(db):
    with pytest.raises(DepartmentNotFoundError):
        PersonnelService(db).create(PersonnelCreate(employee_id="1", name="A", department_id=999))


def test_personnel_move_between_departments(db):
    service = PersonnelService(db)
    icu = DepartmentService(db).create(DepartmentCreate(name="ICU"))
    isolasi = DepartmentService(db).create(DepartmentCreate(name="ISOLASI"))
    person = service.create(PersonnelCreate(employee_id="1", name="A", department_id=icu.id))

    moved = service.update(person.id, PersonnelUpdate(department_id=isolasi.id))

    assert moved.department == "ISOLASI"


def test_clear_department(db):
    service = PersonnelService(db)
    icu = DepartmentService(db).create(DepartmentCreate(name="ICU"))
    person = service.create(PersonnelCreate(employee_id="1", name="A", department_id=icu.id))

    cleared = service.update(person.id, PersonnelUpdate(clear_department=True))

    assert cleared.department_id is None
    assert cleared.department is None


def test_renaming_department_updates_every_person(db):
    """This is the whole point of the master: rename once, not 184 times."""
    departments = DepartmentService(db)
    service = PersonnelService(db)
    department = departments.create(DepartmentCreate(name="Keperawatan"))

    for badge in ("1", "2", "3"):
        service.create(
            PersonnelCreate(employee_id=badge, name=f"A{badge}", department="Keperawatan")
        )
    assert departments.count_personnel(department.id) == 3

    departments.update(department.id, DepartmentUpdate(name="Bidang Keperawatan"))

    assert [p.department for p in service.list()] == ["Bidang Keperawatan"] * 3
