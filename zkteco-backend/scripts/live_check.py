"""Live smoke test against real panels (no HTTP server needed).

Usage::

    python -m scripts.live_check --limit 3

Loads devices from ``devices_input.csv`` if the table is empty, runs the real
device client against a few panels and prints the dashboard table.
"""

from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///./live_check.db")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from pathlib import Path  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.services.device_service import DeviceService  # noqa: E402
from app.services.personnel_service import PersonnelService  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="../devices_input.csv")
    parser.add_argument("--limit", type=int, default=0, help="0 = all devices")
    parser.add_argument("--skip-logs", action="store_true")
    args = parser.parse_args(argv)

    Base.metadata.create_all(bind=engine)

    with SessionLocal() as session:
        service = DeviceService(session)
        personnel = PersonnelService(session)

        if not service.list():
            from scripts.import_devices_csv import main as import_main

            csv_path = Path(args.csv)
            if not csv_path.exists():
                print(f"CSV tidak ditemukan: {csv_path}", file=sys.stderr)
                return 1
            import_main([str(csv_path)])

        devices = service.list()
        if args.limit:
            devices = devices[: args.limit]

        print(f"\nMenghubungi {len(devices)} device...\n")
        for device in devices:
            try:
                info = service.refresh_info(device.id)
                marks = "OK " if info.serial_number else "?? "
                print(
                    f"  {marks}{device.name:<28} {device.ip:<14} "
                    f"serial={info.serial_number or '-':<16} "
                    f"fw={info.firmware_version or '-':<32} "
                    f"users={info.personnel_count if info.personnel_count is not None else '-'}"
                )
            except Exception as exc:
                print(f"  ERR {device.name:<28} {device.ip:<14} {type(exc).__name__}: {exc}")

        print("\n" + "=" * 118)
        header = (
            f"{'Device Name':<28} {'IP Address':<15} {'Serial Number':<18} "
            f"{'Firmware Version':<34} {'Personnel':>9}  Status"
        )
        print(header)
        print("-" * 118)
        for row in service.dashboard_rows():
            print(
                f"{row['device_name']:<28} {row['ip_address']:<15} "
                f"{(row['serial_number'] or '-'):<18} "
                f"{(row['firmware_version'] or '-'):<34} "
                f"{str(row['personnel_count'] if row['personnel_count'] is not None else '-'):>9}"
                f"  {row['status']}"
            )

        if not args.skip_logs:
            print("\nPercobaan pull access log (tabel `transaction`):")
            from app.services.log_service import LogService

            result = LogService(session).pull_device_logs(devices[0].id)
            print(f"  {result}")

        print(f"\nTotal personnel di database pusat: {personnel.count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
