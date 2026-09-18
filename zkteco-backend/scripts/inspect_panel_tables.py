"""Dump the data-table configuration of a panel.

Read-only. Prints, for every table the panel advertises: the table name and
index, and each field's name, index, type and size.

Why this matters: the write path to a panel (Pull SDK ``SetDeviceData``, command
``0x07``) addresses fields by the indexes the panel reports here, and the
``user`` / ``userauthorize`` / ``timezone`` layouts are what a real
ZKAccess replacement has to produce. See docs/DEVICE_PROTOCOL_NOTES.md.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Device
from app.services.device_service import DeviceService

WANTED = ("user", "userauthorize", "timezone", "holiday", "door", "firstcard", "multimcard")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", help="Panel IP; default is the first device in the DB")
    parser.add_argument(
        "--all", action="store_true", help="Print every table, not just the key ones"
    )
    args = parser.parse_args()

    with SessionLocal() as session:
        device = session.scalars(
            select(Device).where(Device.ip == args.ip) if args.ip else select(Device)
        ).first()
        if device is None:
            print("No device found", file=sys.stderr)
            return 1

        print(f"Panel {device.name} @ {device.ip}\n")
        with DeviceService(session).client_for(device) as client:
            # Private in the library, but it is the only way to see the panel's
            # own table/field indexes — and our c3_compat patch already replaces
            # this method with a tolerant version.
            configs = client.panel._get_device_data_cfg()

        for cfg in configs:
            if not args.all and cfg.name not in WANTED:
                continue
            print(f"table {cfg.name!r}  index={cfg.index}  fields={len(cfg.fields)}")
            for field in cfg.fields:
                print(f"    [{field.index:>3}] {field.name:<24} type={field.type}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
