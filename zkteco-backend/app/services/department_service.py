"""Department master data.

ZKAccess keeps these in `DEPARTMENTS` (DEPTID / DEPTNAME / SUPDEPTID). This
backend stores the same hierarchy, so personnel point at a department record
instead of repeating a free-text name.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import Department, Personnel
from app.schemas import DepartmentCreate, DepartmentUpdate

logger = logging.getLogger(__name__)


class DepartmentNotFoundError(LookupError):
    pass


class DuplicateDepartmentError(ValueError):
    pass


class DepartmentCycleError(ValueError):
    """Raised when a department is set as its own ancestor."""


class DepartmentInUseError(ValueError):
    """Raised when deleting a department that still has personnel."""


def _with_parent():
    return selectinload(Department.parent)


class DepartmentService:
    def __init__(self, db: Session):
        self.db = db

    # -- queries -----------------------------------------------------------
    def list(self, *, q: str | None = None) -> list[Department]:
        stmt = select(Department).options(_with_parent()).order_by(Department.name)
        if q:
            stmt = stmt.where(Department.name.ilike(f"%{q}%"))
        return list(self.db.scalars(stmt))

    def get(self, department_id: int) -> Department:
        department = self.db.scalars(
            select(Department)
            .options(_with_parent())
            .where(Department.id == department_id)
            .execution_options(populate_existing=True)
        ).first()
        if department is None:
            raise DepartmentNotFoundError(f"Departemen {department_id} tidak ditemukan")
        return department

    def get_by_name(self, name: str) -> Department | None:
        return self.db.scalars(select(Department).where(Department.name == name.strip())).first()

    def count_personnel(self, department_id: int) -> int:
        return int(
            self.db.scalar(
                select(func.count())
                .select_from(Personnel)
                .where(Personnel.department_id == department_id)
            )
            or 0
        )

    def personnel_counts(self) -> dict[int, int]:
        """All counts in one query, so listing does not fan out."""
        rows = self.db.execute(
            select(Personnel.department_id, func.count())
            .where(Personnel.department_id.is_not(None))
            .group_by(Personnel.department_id)
        ).all()
        return {department_id: count for department_id, count in rows}

    def tree(self) -> list[Department]:
        """Top-level departments with children eagerly loaded recursively."""
        stmt = select(Department).where(Department.parent_id.is_(None)).order_by(Department.name)
        return list(self.db.scalars(stmt))

    # -- writes ------------------------------------------------------------
    def create(self, payload: DepartmentCreate) -> Department:
        data = payload.model_dump()
        parent_id = data.get("parent_id")
        if parent_id is not None:
            self.get(parent_id)  # 404 before writing anything

        department = Department(**data)
        self.db.add(department)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateDepartmentError(
                f"Departemen '{payload.name}' (atau legacy_id-nya) sudah ada"
            ) from exc
        return self.get(department.id)

    def update(self, department_id: int, payload: DepartmentUpdate) -> Department:
        department = self.get(department_id)
        data = payload.model_dump(exclude_unset=True)
        clear_parent = data.pop("clear_parent", False)

        if "parent_id" in data and data["parent_id"] is not None:
            if data["parent_id"] == department_id:
                raise DepartmentCycleError("Departemen tidak bisa menjadi induk dirinya sendiri")
            self.get(data["parent_id"])
            if self._is_descendant(data["parent_id"], department_id):
                raise DepartmentCycleError(
                    "Tidak bisa memindahkan departemen ke bawah turunannya sendiri"
                )
        if clear_parent:
            data["parent_id"] = None

        for field, value in data.items():
            setattr(department, field, value)

        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateDepartmentError("Nama departemen bentrok") from exc
        return self.get(department_id)

    def delete(self, department_id: int) -> None:
        department = self.get(department_id)

        used = self.count_personnel(department_id)
        if used:
            raise DepartmentInUseError(
                f"Departemen '{department.name}' masih dipakai {used} personel; "
                "pindahkan mereka dulu"
            )

        # Re-parent the children and COMMIT before deleting.
        #
        # SQLAlchemy de-associates the children of a deleted parent by setting
        # their FK to NULL, which would silently undo a re-parent done in the
        # same flush. Committing first removes the old department from the
        # children's state entirely, so nothing is nulled afterwards.
        parent_id = department.parent_id
        self.db.execute(
            update(Department)
            .where(Department.parent_id == department_id)
            .values(parent_id=parent_id)
        )
        self.db.commit()
        self.db.expire_all()

        self.db.delete(self.get(department_id))
        self.db.commit()

    # -- helpers -----------------------------------------------------------
    def resolve_or_create(self, name: str | None) -> Department | None:
        """Look up a department by name, creating it when unknown.

        Used by personnel create/update so callers can keep passing a plain
        department name (as the ZKAccess importer does) and still end up linked
        to the master record.
        """
        cleaned = (name or "").strip()
        if not cleaned:
            return None

        existing = self.get_by_name(cleaned)
        if existing is not None:
            return existing

        logger.info("creating department %r on first use", cleaned)
        return self.create(DepartmentCreate(name=cleaned))

    def _is_descendant(self, candidate_id: int, ancestor_id: int) -> bool:
        """True when `candidate_id` is inside the subtree of `ancestor_id`."""
        seen: set[int] = set()
        current = self.db.get(Department, candidate_id)
        while current is not None and current.id not in seen:
            if current.parent_id == ancestor_id:
                return True
            seen.add(current.id)
            current = (
                self.db.get(Department, current.parent_id)
                if current.parent_id is not None
                else None
            )
        return False
