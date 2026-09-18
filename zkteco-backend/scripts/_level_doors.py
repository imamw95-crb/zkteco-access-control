"""TEMPORARY: what does access level TEKNISI/IT actually cover, and who is on each panel?

Read-only. Answers "how many devices does this level span?" — the question that
was answered too narrowly before.
"""

from __future__ import annotations

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccessGroup, AccessGroupDoor, Device, Personnel


def main() -> int:
    with SessionLocal() as session:
        group = session.get(AccessGroup, 1)
        print(f"Access level: id={group.id} name={group.name!r}")

        links = list(
            session.scalars(select(AccessGroupDoor).where(AccessGroupDoor.access_group_id == 1))
        )
        print(f"\ndoors in this level: {len(links)}")

        devices = {d.id: d for d in session.scalars(select(Device))}
        per_device: dict[int, list[int]] = {}
        for link in links:
            per_device.setdefault(link.device_id, []).append(link.door_number)

        print(f"distinct devices: {len(per_device)}\n")
        for device_id, doors in sorted(per_device.items(), key=lambda kv: kv[0]):
            device = devices.get(device_id)
            name = device.name if device else "?"
            ip = device.ip if device else "?"
            print(f"  id={device_id:<4} {name:<26} {ip:<14} doors={sorted(doors)}")

        members = [p for p in session.scalars(select(Personnel))]
        inside = [p for p in members if 1 in p.access_group_ids]
        print(f"\nmembers of this level: {len(inside)}")

        newbie = [p for p in members if p.id in (530, 531)]
        print("\n-- the two test records --")
        for person in newbie:
            print(
                f"  id={person.id} badge={person.employee_id!r} name={person.name!r} "
                f"card={person.card_number!r} levels={person.access_group_ids}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
