"""Check that one person's access matches the dashboard on every panel.

Read-only. This is the honest answer to "did the push land?" — it reads each panel
back instead of trusting the push result, and it checks ``userauthorize``, because
that is what grants access; a ``user`` row on its own opens nothing.

Every panel is read, not only the ones the person's levels reach, because the
interesting failure is a right that is still granted where it should not be. Taking
somebody out of an access level once left their old ``userauthorize`` row on every
panel of the level they left, and the door still opened.

Worth having because panel reads are not always clean: a read can fail with -2
(busy), -107 (connect refused) or -112 (reply larger than the buffer) and be fine a
moment later, so a reported failure deserves a second look before anyone acts on it.

    python -m scripts.verify_person_on_panels --badge 2150141426
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Device, Personnel
from app.services.personnel_service import PersonnelService
from app.services.push_agent import PushAgentClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--badge", required=True, help="Badge/PIN as stored on the panels")
    args = parser.parse_args()

    badge = args.badge.strip()
    client = PushAgentClient()
    if not client.configured:
        print("PUSH_AGENT_URL belum diisi — jalankan agent/zk_push_agent.py dulu.")
        return 2

    with SessionLocal() as session:
        person = session.scalar(select(Personnel).where(Personnel.employee_id == badge))
        if person is None:
            print(f"Tidak ada personel dengan badge '{badge}'")
            return 1
        covered = {
            device.id for device in PersonnelService(session).devices_for_personnel([person.id])
        }
        devices = list(session.scalars(select(Device).order_by(Device.name)))
        name, card = person.name, person.card_number

    print(
        f"{name} (badge {badge}, kartu {card or '-'}) — "
        f"{len(covered)} dari {len(devices)} panel seharusnya memuatnya\n"
    )

    print(f"{'panel':<26} {'ip':<14} {'seharusnya':>10} {'pintu':>7}  hasil")
    print("-" * 78)

    # Three outcomes, kept apart on purpose. "Could not read" is not "not there":
    # conflating them sends somebody chasing a panel that is fine — the same mistake
    # the agent used to make by reporting a failed read-back as a failed write.
    ok: list[str] = []
    stale: list[str] = []  # holds a right it should no longer have
    missing: list[str] = []  # should hold a right it does not have
    unreadable: list[str] = []

    for device in devices:
        should = device.id in covered
        try:
            auths = client.read_table(device.ip, "userauthorize", ["Pin", "AuthorizeDoorId"])
        except Exception as exc:  # noqa: BLE001 - one dead panel must not stop the check
            print(
                f"{device.name:<26} {device.ip:<14} {('ya' if should else '-'):>10} "
                f"{'?':>7}  gagal baca: {exc}"
            )
            unreadable.append(device.name)
            continue

        rows = [row for row in auths if (row.get("Pin") or "").strip() == badge]
        doors = ",".join(sorted(str(row.get("AuthorizeDoorId")) for row in rows)) or "-"

        if should == bool(rows):
            verdict, bucket = "OK", ok
        elif rows:
            verdict, bucket = ">>> MASIH PUNYA HAK, seharusnya tidak <<<", stale
        else:
            verdict, bucket = ">>> BELUM PUNYA HAK, seharusnya ada <<<", missing
        bucket.append(device.name)

        print(
            f"{device.name:<26} {device.ip:<14} {('ya' if should else '-'):>10} "
            f"{doors:>7}  {verdict}"
        )

    print()
    print(f"sesuai : {len(ok)}/{len(devices)}")
    if stale:
        print(f"SISA HAK LAMA — jalankan 'Samakan ke semua panel' : {', '.join(stale)}")
    if missing:
        print(f"BELUM DIBERI — jalankan 'Samakan ke semua panel' : {', '.join(missing)}")
    if unreadable:
        print(f"TIDAK BISA DIBACA (belum tentu salah, ulangi) : {', '.join(unreadable)}")
    if stale or missing or unreadable:
        return 1
    print("Semua panel sesuai: hak yang ada persis yang seharusnya.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
