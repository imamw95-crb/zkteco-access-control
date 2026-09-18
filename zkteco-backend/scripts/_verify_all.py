"""TEMPORARY: does the card actually have a working authorization row everywhere?

A `user` row alone does not open a door — `userauthorize` (time zone + door mask)
is what grants it. This checks both, on every panel the level covers.
"""

from __future__ import annotations

import json
import os
import urllib.request

BASE = "http://localhost:8000"
PIN = "2150141426"
LEVEL_ID = 1
AGENT = os.environ.get("PUSH_AGENT_URL", "http://127.0.0.1:8081")
TOKEN = os.environ.get("PUSH_AGENT_TOKEN", "")


def api(path: str):
    with urllib.request.urlopen(BASE + path, timeout=120) as response:
        return json.load(response)


def agent_get(path: str):
    request = urllib.request.Request(AGENT + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def main() -> int:
    level = api(f"/api/access-groups/{LEVEL_ID}")
    device_ids = sorted({door["device_id"] for door in level["doors"]})
    devices = {d["id"]: d for d in api("/api/devices")}

    print(f"{'device':<26} {'ip':<14} {'user':>6} {'auth':>6}  status")
    print("-" * 72)

    bad = []
    for device_id in device_ids:
        device = devices[device_id]
        name, ip = device["name"], device["ip"]
        try:
            users = agent_get(f"/panels/{ip}/tables/user?fields=Pin,CardNo")["records"]
            auths = agent_get(f"/panels/{ip}/tables/userauthorize?fields=Pin,AuthorizeDoorId")[
                "records"
            ]
        except Exception as exc:  # noqa: BLE001 - one dead panel must not stop the run
            print(f"{name:<26} {ip:<14} {'?':>6} {'?':>6}  gagal baca: {str(exc)[:24]}")
            bad.append(name)
            continue

        has_user = any((r.get("Pin") or "").strip() == PIN for r in users)
        row = next((r for r in auths if (r.get("Pin") or "").strip() == PIN), None)

        if has_user and row is not None:
            status = f"OK (pintu mask {row.get('AuthorizeDoorId')})"
        elif has_user:
            status = ">>> user ADA tapi TANPA authorize - pintu tidak akan terbuka <<<"
            bad.append(name)
        else:
            status = ">>> user TIDAK ADA <<<"
            bad.append(name)

        print(
            f"{name:<26} {ip:<14} {('ADA' if has_user else 'TIDAK'):>6} "
            f"{('ADA' if row else 'TIDAK'):>6}  {status}"
        )

    print()
    if bad:
        print(f"PERLU DITINDAK: {', '.join(bad)}")
    else:
        print(f"Semua {len(device_ids)} panel OK: user + authorize ada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
