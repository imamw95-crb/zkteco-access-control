"""Department master data endpoints.

Mirrors the ZKAccess "Department" screen: a hierarchy of organisational units
that personnel belong to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_department_service
from app.models import Department
from app.schemas import (
    DepartmentCreate,
    DepartmentDetail,
    DepartmentNode,
    DepartmentRead,
    DepartmentUpdate,
)
from app.services.department_service import (
    DepartmentCycleError,
    DepartmentInUseError,
    DepartmentNotFoundError,
    DepartmentService,
    DuplicateDepartmentError,
)

router = APIRouter(prefix="/departments", tags=["departments"])


def _read(department: Department, counts: dict[int, int]) -> DepartmentRead:
    return DepartmentRead(
        id=department.id,
        name=department.name,
        legacy_id=department.legacy_id,
        code=department.code,
        description=department.description,
        parent_id=department.parent_id,
        personnel_count=counts.get(department.id, 0),
    )


@router.get("", response_model=list[DepartmentRead], summary="List departments")
def list_departments(
    q: str | None = Query(None, description="Cari nama departemen"),
    service: DepartmentService = Depends(get_department_service),
):
    counts = service.personnel_counts()
    return [_read(department, counts) for department in service.list(q=q)]


@router.get("/tree", response_model=list[DepartmentNode], summary="Department hierarchy")
def department_tree(service: DepartmentService = Depends(get_department_service)):
    counts = service.personnel_counts()
    all_departments = service.list()
    by_parent: dict[int | None, list[Department]] = {}
    for department in all_departments:
        by_parent.setdefault(department.parent_id, []).append(department)

    def build(department: Department) -> DepartmentNode:
        return DepartmentNode(
            id=department.id,
            name=department.name,
            legacy_id=department.legacy_id,
            parent_id=department.parent_id,
            personnel_count=counts.get(department.id, 0),
            children=[build(child) for child in by_parent.get(department.id, [])],
        )

    return [build(root) for root in by_parent.get(None, [])]


@router.post(
    "",
    response_model=DepartmentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a department",
)
def create_department(
    payload: DepartmentCreate, service: DepartmentService = Depends(get_department_service)
):
    try:
        department = service.create(payload)
    except DepartmentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DuplicateDepartmentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _detail(service, department)


@router.get("/{department_id}", response_model=DepartmentDetail, summary="Department detail")
def get_department(
    department_id: int, service: DepartmentService = Depends(get_department_service)
):
    try:
        department = service.get(department_id)
    except DepartmentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _detail(service, department)


@router.patch("/{department_id}", response_model=DepartmentDetail, summary="Edit a department")
def update_department(
    department_id: int,
    payload: DepartmentUpdate,
    service: DepartmentService = Depends(get_department_service),
):
    try:
        department = service.update(department_id, payload)
    except DepartmentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (DuplicateDepartmentError, DepartmentCycleError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _detail(service, department)


@router.delete(
    "/{department_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a department",
)
def delete_department(
    department_id: int, service: DepartmentService = Depends(get_department_service)
):
    try:
        service.delete(department_id)
    except DepartmentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DepartmentInUseError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


def _detail(service: DepartmentService, department: Department) -> DepartmentDetail:
    children = [d for d in service.list() if d.parent_id == department.id]
    counts = service.personnel_counts()
    return DepartmentDetail(
        id=department.id,
        name=department.name,
        legacy_id=department.legacy_id,
        code=department.code,
        description=department.description,
        parent_id=department.parent_id,
        personnel_count=counts.get(department.id, 0),
        full_path=department.full_path,
        parent_name=department.parent.name if department.parent else None,
        children=[_read(child, counts) for child in children],
    )
