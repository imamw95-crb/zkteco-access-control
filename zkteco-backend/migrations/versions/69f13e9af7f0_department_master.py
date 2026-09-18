"""department master

Revision ID: 69f13e9af7f0
Revises: 907835756e6e
Create Date: 2026-09-16 13:24:10.669725
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "69f13e9af7f0"
down_revision: str | None = "907835756e6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("legacy_id", sa.Integer(), nullable=True),
        sa.Column("code", sa.String(length=32), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["departments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_departments_parent_id"), "departments", ["parent_id"], unique=False)

    # The old `personnel.department` text column holds the only copy of the
    # department data, so promote the distinct names into the new master BEFORE
    # dropping it — otherwise every person loses their department.
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    names = [
        row[0].strip()
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT department FROM personnel "
                "WHERE department IS NOT NULL AND TRIM(department) <> ''"
            )
        ).fetchall()
    ]
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        bind.execute(
            sa.text(
                "INSERT INTO departments (name, created_at, updated_at) "
                "VALUES (:name, :created, :updated)"
            ),
            {"name": name, "created": now, "updated": now},
        )

    # Batch mode so SQLite (which cannot ALTER constraints) also works.
    with op.batch_alter_table("personnel") as batch:
        batch.add_column(sa.Column("department_id", sa.Integer(), nullable=True))
        batch.create_index("ix_personnel_department_id", ["department_id"])
        batch.create_foreign_key(
            "fk_personnel_department_id",
            "departments",
            ["department_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # Link every person to their department while the text value still exists.
    bind.execute(
        sa.text(
            "UPDATE personnel SET department_id = "
            "(SELECT d.id FROM departments d WHERE d.name = TRIM(personnel.department)) "
            "WHERE department IS NOT NULL AND TRIM(department) <> ''"
        )
    )

    with op.batch_alter_table("personnel") as batch:
        batch.drop_column("department")


def downgrade() -> None:
    # Restore the text column and refill it from the master before tearing the
    # master down, so a downgrade does not lose department names either.
    with op.batch_alter_table("personnel") as batch:
        batch.add_column(sa.Column("department", sa.VARCHAR(length=128), nullable=True))

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE personnel SET department = "
            "(SELECT d.name FROM departments d WHERE d.id = personnel.department_id) "
            "WHERE department_id IS NOT NULL"
        )
    )

    with op.batch_alter_table("personnel") as batch:
        batch.drop_constraint("fk_personnel_department_id", type_="foreignkey")
        batch.drop_index("ix_personnel_department_id")
        batch.drop_column("department_id")

    op.drop_index(op.f("ix_departments_parent_id"), table_name="departments")
    op.drop_table("departments")
