"""Reconcile the migrated data against the ZKAccess source.

Compares set-by-set (not just counts) so a silent mix-up would be caught:

    python -m scripts.verify_migration

Exit code 0 when everything matches, 1 otherwise.
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccessGroup, AccessGroupDoor, Device, Personnel, PersonnelAccessGroup
from app.services.zkaccess_source import (
    ZKAccessConfig,
    ZKAccessSource,
    ZKAccessSourceError,
)

OK = "  OK  "
BAD = " BEDA "


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def compare(label: str, source_value, target_value) -> bool:
    same = source_value == target_value
    print(f"  [{OK if same else BAD}] {label:<44} sumber={source_value} target={target_value}")
    return same


def main() -> int:
    try:
        config = ZKAccessConfig.from_env()
    except ZKAccessSourceError as exc:
        print(f"Konfigurasi: {exc}", file=sys.stderr)
        return 1

    failures = 0
    print(f"Rekonsiliasi {config.server} / {config.database}  ->  target\n")

    with ZKAccessSource(config) as src, SessionLocal() as session:
        # ---- devices -----------------------------------------------------
        src_devices = {_text(r["IP"]) for r in src.devices() if _text(r["IP"])}
        dst_devices = {d.ip for d in session.scalars(select(Device))}
        print("Devices")
        failures += not compare("jumlah device", len(src_devices), len(dst_devices))
        missing = src_devices - dst_devices
        extra = dst_devices - src_devices
        failures += not compare("device hilang di target", 0, len(missing))
        if missing:
            print(f"          -> {sorted(missing)}")
        if extra:
            print(f"          catatan: target punya device tambahan: {sorted(extra)}")

        # ---- personnel ---------------------------------------------------
        src_people = {_text(r["Badgenumber"]) for r in src.personnel() if _text(r["Badgenumber"])}
        dst_people = {p.employee_id for p in session.scalars(select(Personnel))}
        print("\nPersonel")
        failures += not compare("jumlah personel", len(src_people), len(dst_people))
        failures += not compare("personel hilang di target", 0, len(src_people - dst_people))
        failures += not compare("personel berlebih di target", 0, len(dst_people - src_people))

        src_cards = {
            _text(r["CardNo"]): _text(r["Badgenumber"])
            for r in src.personnel()
            if _text(r["CardNo"])
        }
        dst_by_badge = {p.employee_id: p for p in session.scalars(select(Personnel))}
        card_mismatch = sum(
            1
            for card, badge in src_cards.items()
            if badge in dst_by_badge and dst_by_badge[badge].card_number != card
        )
        failures += not compare("kartu tidak cocok", 0, card_mismatch)

        # ---- access groups -----------------------------------------------
        src_groups = {_text(r["level_name"]) for r in src.access_groups() if _text(r["level_name"])}
        dst_groups = {g.name for g in session.scalars(select(AccessGroup))}
        print("\nGrup akses")
        failures += not compare("jumlah grup", len(src_groups), len(dst_groups))
        failures += not compare("grup hilang di target", 0, len(src_groups - dst_groups))

        # ---- group -> door -----------------------------------------------
        # group_doors() returns acclevelset_id, not the group name, so resolve
        # it through the levelset map built above.
        levelset_to_name = {int(r["id"]): _text(r["level_name"]) for r in src.access_groups()}
        src_pairs = {
            (
                levelset_to_name.get(int(r["acclevelset_id"])),
                _text(r["device_ip"]),
                int(r["door_no"] or 1),
            )
            for r in src.group_doors()
            if _text(r["device_ip"])
        }
        src_pairs = {p for p in src_pairs if p[0]}
        # Rebuild the same tuples from the target side.
        dst_pairs = set()
        for link in session.scalars(select(AccessGroupDoor)):
            group = session.get(AccessGroup, link.access_group_id)
            device = session.get(Device, link.device_id)
            if group and device:
                dst_pairs.add((group.name, device.ip, link.door_number))

        print("\nPemetaan grup -> pintu")
        failures += not compare(
            "jumlah pasangan (grup, device, pintu)", len(src_pairs), len(dst_pairs)
        )
        failures += not compare("pasangan hilang di target", 0, len(src_pairs - dst_pairs))

        # ---- memberships -------------------------------------------------
        src_members = {
            (_text(r["Badgenumber"]), int(r["acclevelset_id"]))
            for r in src.group_members()
            if _text(r["Badgenumber"])
        }
        group_name_to_id = {g.name: g.id for g in session.scalars(select(AccessGroup))}
        person_id_by_badge = {p.employee_id: p.id for p in session.scalars(select(Personnel))}

        dst_members = set()
        for link in session.scalars(select(PersonnelAccessGroup)):
            person = session.get(Personnel, link.personnel_id)
            group = session.get(AccessGroup, link.access_group_id)
            if person and group:
                dst_members.add((person.employee_id, group.name))

        src_members_named = {
            (badge, levelset_to_name.get(levelset)) for badge, levelset in src_members
        }
        src_members_named = {(b, g) for b, g in src_members_named if g}

        print("\nKeanggotaan personel -> grup")
        failures += not compare("jumlah keanggotaan", len(src_members_named), len(dst_members))
        failures += not compare(
            "keanggotaan hilang di target", 0, len(src_members_named - dst_members)
        )

        # ---- sanity: multi-group people ----------------------------------
        per_person: dict[str, int] = {}
        for badge, _group in dst_members:
            per_person[badge] = per_person.get(badge, 0) + 1
        max_groups = max(per_person.values()) if per_person else 0
        multi = sum(1 for n in per_person.values() if n > 1)
        print("\nSanity check")
        print(f"  [  OK  ] orang dengan >1 grup{'':<25} {multi}")
        print(f"  [  OK  ] grup terbanyak untuk 1 orang{'':<18} {max_groups}")
        unused = group_name_to_id.keys() - {g for _b, g in dst_members}
        print(f"  [  OK  ] grup tanpa anggota{'':<28} {len(unused)}")
        del person_id_by_badge  # only used for bookkeeping above

    print()
    if failures:
        print(f"RESULT: {failures} ketidakcocokan ditemukan")
        return 1
    print("RESULT: OK - data target cocok dengan sumber ZKAccess")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
