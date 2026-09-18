"""TEMPORARY: push one person to every panel their access level covers. Read-mostly.

The dashboard only pushes per device, so a person can sit on the door the
operator happened to look at while the other 19 panels they were granted stay
stale. This walks the whole level.

Writes are limited with `personnel_ids=<id>`, so only that one Pin is sent and
the hundreds of other users already on each panel are never rewritten.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
PERSON_ID = 531
LEVEL_ID = 1
AGENT = os.environ.get("PUSH_AGENT_URL", "http://127.0.0.1:8081")
TOKEN = os.environ.get("PUSH_AGENT_TOKEN", "")


def api(path: str, method: str = "GET"):
    request = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def agent_get(path: str):
    request = urllib.request.Request(AGENT + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def main() -> int:
    levels = api(f"/api/access-groups/{LEVEL_ID}")
    device_ids = sorted({door["device_id"] for door in levels["doors"]})
    devices = {d["id"]: d for d in api("/api/devices")}

    print(f"Level {levels['name']!r} mencakup {len(device_ids)} device\n")
    print(f"{'device':<26} {'ip':<14} {'push':>10}  {'di panel':>8}")
    print("-" * 66)

    ok = failed = 0
    for device_id in device_ids:
        device = devices[device_id]
        name, ip = device["name"], device["ip"]

        try:
            result = api(
                f"/api/devices/{device_id}/personnel/sync?personnel_ids={PERSON_ID}",
                method="POST",
            )
            wrote = result.get("written", {}).get("user", 0)
            note = f"{wrote} user"
            ok += 1
        except urllib.error.HTTPError as exc:
            note = f"HTTP {exc.code}"
            failed += 1
        except Exception as exc:  # noqa: BLE001 - one dead panel must not stop the run
            note = str(exc)[:30]
            failed += 1

        try:
            rows = agent_get(f"/panels/{ip}/tables/user?fields=Pin,CardNo")["records"]
            present = any((r.get("Pin") or "").strip() == "2150141426" for r in rows)
            verified = f"{'ADA' if present else 'TIDAK'} ({len(rows)})"
        except Exception as exc:  # noqa: BLE001
            verified = f"gagal: {str(exc)[:18]}"

        print(f"{name:<26} {ip:<14} {note:>10}  {verified:>8}")

    print(f"\nselesai: {ok} push berhasil, {failed} gagal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
