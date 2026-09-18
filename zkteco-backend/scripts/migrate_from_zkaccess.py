"""Migrate ZKAccess master data into this backend.

Reads from the legacy ZKAccess SQL Server (read-only) and writes into the new
database: devices, access groups, group->door mapping, personnel, and
person->group memberships. Access logs are deliberately NOT migrated.

    set ZKACCESS_SERVER=10.100.1.100\\SQLEXPRESS
    set ZKACCESS_USER=sa
    set ZKACCESS_PASSWORD=...
    python -m scripts.migrate_from_zkaccess --dry-run
    python -m scripts.migrate_from_zkaccess

Idempotent: rows are matched on natural keys (device IP+port, group name,
employee_id), so re-running updates instead of duplicating.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import (
    AccessGroup,
    AccessGroupDoor,
    Department,
    Device,
    Personnel,
    PersonnelAccessGroup,
)
from app.services.zkaccess_source import (
    ZKAccessConfig,
    ZKAccessSource,
    ZKAccessSourceError,
)


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return None


class Stats:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.warnings: list[str] = []

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def report(self, title: str) -> None:
        print(f"\n{title}")
        print("-" * 62)
        for key in sorted(self.counts):
            print(f"  {key:<34} {self.counts[key]:>7,}")
        if self.warnings:
            print(f"\n  Peringatan ({len(self.warnings)}):")
            for message in self.warnings[:25]:
                print(f"    - {message}")
            if len(self.warnings) > 25:
                print(f"    ... dan {len(self.warnings) - 25} lainnya")


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def migrate_devices(session: Session, src: ZKAccessSource, stats: Stats) -> dict[str, Device]:
    by_ip: dict[str, Device] = {}
    for row in src.devices():
        ip = _text(row["IP"])
        if not ip:
            stats.warn(f"device ID {row['ID']} tanpa IP, dilewati")
            continue
        port = int(row["Port"] or 4370)

        device = session.scalars(select(Device).where(Device.ip == ip, Device.port == port)).first()
        created = device is None
        if created:
            device = Device(name=_text(row["MachineAlias"]) or ip, ip=ip, port=port)
            session.add(device)
            session.flush()

        device.name = _text(row["MachineAlias"]) or device.name
        device.serial_number = _text(row["sn"]) or device.serial_number
        device.firmware_version = _text(row["FirmwareVersion"]) or device.firmware_version
        device.password = _text(row["CommPassword"]) or device.password
        device.lock_count = int(row["door_count"] or 0) or device.lock_count
        device.reader_count = int(row["reader_count"] or 0) or device.reader_count
        device.max_user_count = int(row["max_user_count"] or 0) or device.max_user_count
        if row["usercount"] is not None:
            device.personnel_count = int(row["usercount"])

        by_ip[ip] = device
        stats.add("device dibuat" if created else "device diperbarui")

    session.flush()
    return by_ip


def migrate_groups(
    session: Session, src: ZKAccessSource, by_ip: dict[str, Device], stats: Stats
) -> dict[int, AccessGroup]:
    by_levelset: dict[int, AccessGroup] = {}

    for row in src.access_groups():
        name = _text(row["level_name"])
        if not name:
            stats.warn(f"levelset {row['id']} tanpa nama, dilewati")
            continue

        group = session.scalars(select(AccessGroup).where(AccessGroup.name == name)).first()
        created = group is None
        if created:
            group = AccessGroup(name=name)
            session.add(group)
            session.flush()

        group.device_timezone_id = int(row["level_timeseg_id"] or 1)
        by_levelset[int(row["id"])] = group
        stats.add("grup dibuat" if created else "grup diperbarui")

    session.flush()

    # group -> (device, door)
    door_numbers: dict[int, set[int]] = {}
    for row in src.group_doors():
        group = by_levelset.get(int(row["acclevelset_id"]))
        if group is None:
            continue
        device = by_ip.get(_text(row["device_ip"]) or "")
        if device is None:
            stats.warn(
                f"grup {group.name}: accdoor {row['accdoor_id']} menunjuk device "
                f"tak dikenal ({row['device_ip']})"
            )
            continue

        door_number = int(row["door_no"] or 1)
        exists = session.scalars(
            select(AccessGroupDoor).where(
                AccessGroupDoor.access_group_id == group.id,
                AccessGroupDoor.device_id == device.id,
                AccessGroupDoor.door_number == door_number,
            )
        ).first()
        if exists is None:
            session.add(
                AccessGroupDoor(
                    access_group_id=group.id, device_id=device.id, door_number=door_number
                )
            )
            stats.add("pintu grup ditambah")
        else:
            stats.add("pintu grup sudah ada")

        door_numbers.setdefault(group.id, set()).add(door_number)

    # Display-only summary on the group row; access_group_doors stays authoritative.
    for group_id, numbers in door_numbers.items():
        for group in by_levelset.values():
            if group.id == group_id:
                group.door_numbers = ",".join(str(n) for n in sorted(numbers))
                break

    session.flush()
    return by_levelset


def migrate_departments(
    session: Session, src: ZKAccessSource, stats: Stats
) -> dict[str, Department]:
    """Import DEPARTMENTS with its hierarchy (SUPDEPTID -> parent).

    Two passes: insert every row first, then wire up the parents, so a child
    whose parent appears later in the list still links correctly.
    """
    by_legacy_id: dict[int, Department] = {}
    by_name: dict[str, Department] = {}
    pending_parents: list[tuple[Department, int]] = []

    rows = src.departments()
    for row in rows:
        name = _text(row["DEPTNAME"])
        if not name:
            stats.warn(f"deptid {row['DEPTID']} tanpa nama, dilewati")
            continue
        legacy_id = int(row["DEPTID"])

        department = session.scalars(select(Department).where(Department.name == name)).first()
        created = department is None
        if created:
            department = Department(name=name)
            session.add(department)
            session.flush()

        department.legacy_id = legacy_id
        code = _text(row.get("code"))
        if code:
            department.code = code

        parent_legacy = row.get("SUPDEPTID")
        if parent_legacy is not None and int(parent_legacy) not in (0, legacy_id):
            pending_parents.append((department, int(parent_legacy)))

        by_legacy_id[legacy_id] = department
        by_name[name] = department
        stats.add("departemen dibuat" if created else "departemen diperbarui")

    session.flush()

    for department, parent_legacy in pending_parents:
        parent = by_legacy_id.get(parent_legacy)
        if parent is None:
            stats.warn(f"departemen '{department.name}' punya induk tak dikenal {parent_legacy}")
            continue
        department.parent_id = parent.id

    session.flush()
    return by_name


def migrate_personnel(
    session: Session,
    src: ZKAccessSource,
    stats: Stats,
    departments: dict[str, Department] | None = None,
) -> dict[str, Personnel]:
    by_employee_id: dict[str, Personnel] = {}
    used_cards: dict[str, str] = {}

    # Existing card numbers, so a clash is detected before it hits the unique index.
    for existing in session.scalars(select(Personnel)):
        if existing.card_number:
            used_cards[existing.card_number] = existing.employee_id

    for row in src.personnel():
        employee_id = _text(row["Badgenumber"])
        if not employee_id:
            stats.warn(f"USERID {row['USERID']} tanpa Badgenumber, dilewati")
            continue

        name = _text(row["Name"]) or ""
        lastname = _text(row["lastname"]) or ""
        if lastname and lastname.lower() not in name.lower():
            name = f"{name} {lastname}".strip()
        if not name:
            name = f"User {employee_id}"
            stats.warn(f"badge {employee_id} tanpa nama")

        person = by_employee_id.get(employee_id)
        if person is None:
            person = session.scalars(
                select(Personnel).where(Personnel.employee_id == employee_id)
            ).first()
        created = person is None
        if created:
            person = Personnel(employee_id=employee_id, name=name)
            session.add(person)
            session.flush()

        person.name = name
        person.pin = employee_id
        department_name = _text(row["DEPTNAME"])
        if department_name:
            department = (departments or {}).get(department_name)
            if department is None:
                department = session.scalars(
                    select(Department).where(Department.name == department_name)
                ).first()
            if department is None:
                department = Department(name=department_name)
                session.add(department)
                session.flush()
                stats.warn(f"departemen '{department_name}' dibuat dari data personel")
            person.department_id = department.id
        person.valid_from = _as_datetime(row["acc_startdate"]) or person.valid_from
        person.valid_until = _as_datetime(row["acc_enddate"]) or person.valid_until

        card = _text(row["CardNo"])
        if card:
            owner = used_cards.get(card)
            if owner and owner != employee_id:
                stats.warn(
                    f"kartu {card} dipakai {employee_id} tapi sudah dimiliki {owner} — dilewati"
                )
            else:
                person.card_number = card
                used_cards[card] = employee_id

        by_employee_id[employee_id] = person
        stats.add("personel dibuat" if created else "personel diperbarui")

    session.flush()
    return by_employee_id


def migrate_memberships(
    session: Session,
    src: ZKAccessSource,
    by_employee_id: dict[str, Personnel],
    by_levelset: dict[int, AccessGroup],
    stats: Stats,
) -> None:
    for row in src.group_members():
        group = by_levelset.get(int(row["acclevelset_id"]))
        employee_id = _text(row["Badgenumber"])
        person = by_employee_id.get(employee_id or "")
        if group is None or person is None:
            stats.add("keanggotaan dilewati")
            continue

        exists = session.scalars(
            select(PersonnelAccessGroup).where(
                PersonnelAccessGroup.personnel_id == person.id,
                PersonnelAccessGroup.access_group_id == group.id,
            )
        ).first()
        if exists is None:
            session.add(PersonnelAccessGroup(personnel_id=person.id, access_group_id=group.id))
            stats.add("keanggotaan ditambah")
        else:
            stats.add("keanggotaan sudah ada")

    session.flush()


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="jangan commit, hanya lapor")
    parser.add_argument("--database-url", help="URL database tujuan (default dari env)")
    args = parser.parse_args(argv)

    if args.database_url:
        import os

        os.environ["DATABASE_URL"] = args.database_url

    try:
        config = ZKAccessConfig.from_env()
    except ZKAccessSourceError as exc:
        print(f"Konfigurasi ZKAccess: {exc}", file=sys.stderr)
        return 1

    mode = "DRY-RUN (tidak ada yang ditulis)" if args.dry_run else "MENULIS ke database"
    print("Migrasi ZKAccess -> backend baru")
    print(f"  sumber : {config.server} / {config.database}")
    print(f"  mode   : {mode}")
    print()

    Base.metadata.create_all(bind=engine)

    stats = Stats()
    try:
        with ZKAccessSource(config) as src:
            with SessionLocal() as session:
                by_ip = migrate_devices(session, src, stats)
                departments = migrate_departments(session, src, stats)
                by_levelset = migrate_groups(session, src, by_ip, stats)
                by_employee_id = migrate_personnel(session, src, stats, departments)
                migrate_memberships(session, src, by_employee_id, by_levelset, stats)

                if args.dry_run:
                    session.rollback()
                else:
                    session.commit()
    except ZKAccessSourceError as exc:
        print(f"Gagal: {exc}", file=sys.stderr)
        return 1

    stats.report("Ringkasan")
    if args.dry_run:
        print("\nDRY-RUN selesai: semua perubahan dibatalkan (rollback).")
    else:
        print("\nSelesai. Jalankan ulang aman (idempotent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
