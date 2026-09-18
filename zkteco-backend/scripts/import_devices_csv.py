"""Import devices from a CSV file (columns: name,ip[,port,password,location,area]).

Example::

    python -m scripts.import_devices_csv devices_input.csv

Existing rows (same ip:port) are updated instead of duplicated.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from app.database import Base, SessionLocal, engine
from app.schemas import DeviceCreate, DeviceUpdate
from app.services.device_service import DeviceService, DuplicateDeviceError


def read_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            normalised = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            name = normalised.get("name") or normalised.get("device name") or ""
            ip = normalised.get("ip") or normalised.get("ip address") or ""
            if not ip:
                continue
            rows.append(
                {
                    "name": name or ip,
                    "ip": ip,
                    "port": int(normalised.get("port") or 4370),
                    "password": normalised.get("password") or None,
                    "location": normalised.get("location") or None,
                    "area": normalised.get("area") or None,
                }
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would change without writing"
    )
    args = parser.parse_args(argv)

    if not args.csv_path.exists():
        print(f"File tidak ditemukan: {args.csv_path}", file=sys.stderr)
        return 1

    rows = read_rows(args.csv_path)
    if not rows:
        print("Tidak ada baris dengan kolom 'ip' di CSV.", file=sys.stderr)
        return 1

    Base.metadata.create_all(bind=engine)

    created = updated = 0
    with SessionLocal() as session:
        service = DeviceService(session)
        for row in rows:
            existing = service.get_by_ip(row["ip"], row["port"])
            if existing is None:
                if not args.dry_run:
                    try:
                        service.create(DeviceCreate(**row))
                    except DuplicateDeviceError as exc:
                        print(f"  skip {row['ip']}: {exc}")
                        continue
                created += 1
                print(f"  + {row['name']:<28} {row['ip']}")
            else:
                changed = {
                    k: v
                    for k, v in row.items()
                    if v is not None and getattr(existing, k, None) != v
                }
                if changed:
                    if not args.dry_run:
                        service.update(existing.id, DeviceUpdate(**changed))
                    updated += 1
                    print(f"  ~ {row['name']:<28} {row['ip']} ({', '.join(changed)})")

    verb = "would be" if args.dry_run else ""
    print(f"\n{len(rows)} baris CSV: {created} {verb} dibuat, {updated} {verb} diperbarui.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
