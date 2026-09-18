"""Smoke-test the ZKAccess read-only source.

set ZKACCESS_SERVER=10.100.1.100\\SQLEXPRESS
set ZKACCESS_USER=sa
set ZKACCESS_PASSWORD=...
python -m scripts.check_zkaccess_source
"""

from __future__ import annotations

import sys

from app.services.zkaccess_source import (
    WriteAttemptedError,
    ZKAccessConfig,
    ZKAccessSource,
    ZKAccessSourceError,
)


def main() -> int:
    try:
        config = ZKAccessConfig.from_env()
    except ZKAccessSourceError as exc:
        print(f"Konfigurasi: {exc}", file=sys.stderr)
        return 1

    print(f"Target : {config.server} / {config.database}")
    print(f"Config : {config!r}\n")

    try:
        with ZKAccessSource(config) as src:
            print("Versi SQL:", src.server_version())

            print("\n-- jumlah baris --")
            for table, count in src.stats().items():
                print(f"   {table:<26} {count:>8,}")

            devices = src.devices()
            print(f"\n-- devices ({len(devices)}) --")
            for d in devices[:4]:
                print(
                    f"   {d['MachineAlias']:<26} {d['IP']:<14} "
                    f"sn={d['sn']} users={d['usercount']} doors={d['door_count']}"
                )
            if len(devices) > 4:
                print(f"   ... dan {len(devices) - 4} lainnya")

            people = src.personnel()
            print(f"\n-- personel ({len(people)}) --")
            for p in people[:4]:
                print(
                    f"   badge={p['Badgenumber']:<12} userid={p['USERID']:<6} "
                    f"{str(p['Name'])[:28]:<28} card={p['CardNo']} dept={p['DEPTNAME']}"
                )

            groups = src.access_groups()
            doors = src.group_doors()
            members = src.group_members()
            print("\n-- grup akses --")
            print(f"   grup          : {len(groups)}")
            print(f"   grup x pintu  : {len(doors)}")
            print(f"   grup x orang  : {len(members)}")

            orphan_doors = [d for d in doors if not d.get("device_ip")]
            print(f"   pintu tanpa device: {len(orphan_doors)}")

            # Safety check: a write must be refused.
            print("\n-- uji pengaman read-only --")
            try:
                src._query("DELETE FROM USERINFO")
                print("   GAGAL: statement tulis tidak ditolak!")
                return 1
            except WriteAttemptedError as exc:
                print(f"   OK, ditolak: {exc}")

    except ZKAccessSourceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("\nRESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
