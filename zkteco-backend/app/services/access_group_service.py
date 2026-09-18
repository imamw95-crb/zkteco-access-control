"""Access level (access group) management.

ZKAccess calls these "access levels": a named set of doors, plus the people who
hold it. This service manages both halves of that relationship, which live in
``access_group_doors`` and ``personnel_access_groups``.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import AccessGroup, AccessGroupDoor, Device, Personnel, PersonnelAccessGroup
from app.schemas import AccessGroupCreate, AccessGroupDoorCreate, AccessGroupUpdate

logger = logging.getLogger(__name__)


class AccessGroupNotFoundError(LookupError):
    pass


class DuplicateAccessGroupError(ValueError):
    pass


def _with_relations():
    return (
        selectinload(AccessGroup.doors).selectinload(AccessGroupDoor.device),
        selectinload(AccessGroup.member_links).selectinload(PersonnelAccessGroup.personnel),
    )


def _group_query(*criteria):
    """Build an AccessGroup query that always re-reads loaded collections.

    Without ``populate_existing`` SQLAlchemy returns the instance already in the
    identity map and leaves its already-loaded collections untouched, so a
    membership added earlier in the same session would still count as zero — and
    the delete-orphan cascade would not see the children it has to remove.
    """
    return (
        select(AccessGroup)
        .options(*_with_relations())
        .where(*criteria)
        .execution_options(populate_existing=True)
    )


class AccessGroupService:
    def __init__(self, db: Session):
        self.db = db

    # -- CRUD --------------------------------------------------------------
    def list(self) -> list[AccessGroup]:
        stmt = _group_query().order_by(AccessGroup.name)
        return list(self.db.scalars(stmt))

    def get(self, group_id: int) -> AccessGroup:
        group = self.db.scalars(_group_query(AccessGroup.id == group_id)).first()
        if group is None:
            raise AccessGroupNotFoundError(f"Access level {group_id} tidak ditemukan")
        return group

    def get_by_name(self, name: str) -> AccessGroup | None:
        return self.db.scalars(_group_query(AccessGroup.name == name)).first()

    def create(self, payload: AccessGroupCreate) -> AccessGroup:
        group = AccessGroup(**payload.model_dump())
        self.db.add(group)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateAccessGroupError(
                f"Access level dengan nama '{payload.name}' sudah ada"
            ) from exc
        return self.get(group.id)

    def update(self, group_id: int, payload: AccessGroupUpdate) -> AccessGroup:
        group = self.get(group_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(group, field, value)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateAccessGroupError("Nama access level bentrok") from exc
        return self.get(group_id)

    def delete(self, group_id: int) -> None:
        group = self.get(group_id)
        self.db.delete(group)
        self.db.commit()

    # -- doors -------------------------------------------------------------
    def add_door(self, group_id: int, payload: AccessGroupDoorCreate) -> AccessGroupDoor:
        group = self.get(group_id)
        device = self.db.get(Device, payload.device_id)
        if device is None:
            raise AccessGroupNotFoundError(f"Device {payload.device_id} tidak ditemukan")

        existing = self.db.scalars(
            select(AccessGroupDoor).where(
                AccessGroupDoor.access_group_id == group.id,
                AccessGroupDoor.device_id == device.id,
                AccessGroupDoor.door_number == payload.door_number,
            )
        ).first()
        if existing is not None:
            return existing

        link = AccessGroupDoor(
            access_group_id=group.id,
            device_id=device.id,
            door_number=payload.door_number,
        )
        self.db.add(link)
        self.db.commit()
        self.db.refresh(link)
        return link

    def remove_door(self, group_id: int, link_id: int) -> None:
        self.get(group_id)  # 404 if the group is gone
        link = self.db.get(AccessGroupDoor, link_id)
        if link is None or link.access_group_id != group_id:
            raise AccessGroupNotFoundError(f"Pintu {link_id} tidak ada di access level {group_id}")
        self.db.delete(link)
        self.db.commit()

    # -- members -----------------------------------------------------------
    def members(self, group_id: int) -> list[Personnel]:
        group = self.get(group_id)
        return sorted(
            (link.personnel for link in group.member_links if link.personnel is not None),
            key=lambda p: p.employee_id,
        )

    def add_members(
        self,
        group_id: int,
        *,
        personnel_ids: list[int] | None = None,
        employee_ids: list[str] | None = None,
    ) -> dict:
        """Attach people to the group. Existing memberships are skipped."""
        group = self.get(group_id)

        wanted: list[Personnel] = []
        skipped = 0

        for person_id in personnel_ids or []:
            person = self.db.get(Personnel, person_id)
            if person is None:
                skipped += 1
            else:
                wanted.append(person)

        for badge in employee_ids or []:
            person = self.db.scalars(
                select(Personnel).where(Personnel.employee_id == badge)
            ).first()
            if person is None:
                skipped += 1
            else:
                wanted.append(person)

        current = {link.personnel_id for link in group.member_links}
        added = 0
        for person in wanted:
            if person.id in current:
                skipped += 1
                continue
            self.db.add(PersonnelAccessGroup(personnel_id=person.id, access_group_id=group.id))
            current.add(person.id)
            added += 1

        self.db.commit()
        return {"access_group_id": group.id, "added": added, "skipped": skipped}

    def remove_member(self, group_id: int, personnel_id: int) -> None:
        self.get(group_id)
        link = self.db.scalars(
            select(PersonnelAccessGroup).where(
                PersonnelAccessGroup.access_group_id == group_id,
                PersonnelAccessGroup.personnel_id == personnel_id,
            )
        ).first()
        if link is None:
            raise AccessGroupNotFoundError(
                f"Personel {personnel_id} bukan anggota access level {group_id}"
            )
        self.db.delete(link)
        self.db.commit()

    # -- extras ------------------------------------------------------------
    def refresh_door_numbers_cache(self, group_id: int) -> AccessGroup:
        """Keep the display-only `door_numbers` string in step with the real mapping."""
        group = self.get(group_id)
        numbers = sorted({link.door_number for link in group.doors})
        group.door_numbers = ",".join(str(n) for n in numbers) if numbers else ""
        self.db.commit()
        return group
