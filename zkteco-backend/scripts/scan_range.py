"""Scan an IP range for C3 panels — the CLI face of ``POST /api/scan``.

Useful before the backend is running, or on a host that cannot open the dashboard:

    python -m scripts.scan_range 10.100.1.0/24 192.168.1.0/24
    python -m scripts.scan_range 10.100.1.0/24 --no-identify --timeout 0.5
    python -m scripts.scan_range 10.100.1.12 10.100.1.20-10.100.1.30

Only the subnets in ``SCAN_ALLOWED_NETWORKS`` may be scanned (default
``10.100.1.0/24,192.168.1.0/24``), so a typo cannot turn into a sweep of the whole
hospital network. Nothing here writes to a panel: it is a TCP connect, then a
read-only identity check.

Exit codes:
    0  at least one host answered on the panel port
    1  nothing answered, an argument was rejected, or the range is not allowed
"""

from __future__ import annotations

import argparse
import sys

from app.database import SessionLocal
from app.schemas import NetworkScanRequest
from app.services.network_scan import NetworkScanService, ScanRangeError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ranges", nargs="+", help="CIDR, IP tunggal, atau rentang a-b")
    parser.add_argument("--port", type=int, default=4370)
    parser.add_argument("--timeout", type=float, default=1.0, help="timeout TCP per host (detik)")
    parser.add_argument("--workers", type=int, default=64, help="probe TCP paralel")
    parser.add_argument(
        "--no-identify",
        dest="identify",
        action="store_false",
        help="jangan baca serial/firmware (jauh lebih cepat, tapi port terbuka bisa bukan panel)",
    )
    parser.add_argument(
        "--local-address",
        default=None,
        help="paksa alamat sumber, kalau host ini punya beberapa NIC",
    )
    args = parser.parse_args(argv)

    request = NetworkScanRequest(
        ranges=args.ranges,
        port=args.port,
        timeout=args.timeout,
        workers=args.workers,
        identify=args.identify,
        local_address=args.local_address,
    )

    for item in NetworkScanService.local_networks():
        where = item["local_address"] or "TIDAK ADA RUTE"
        print(f"Rute ke {item['range']:<20} lewat {where}")
    print()

    db = SessionLocal()
    try:
        service = NetworkScanService(db)
        hosts: list[dict] = []
        summary: dict = {}
        try:
            for event in service.iter_scan(request):
                if event["type"] == "progress":
                    print(
                        f"\r  {event['range']}: {event['done']}/{event['total']} host dipindai, "
                        f"{event['open']} port terbuka",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
                elif event["type"] == "host":
                    hosts.append(event)
                else:
                    summary = event
        except ScanRangeError as exc:
            print(f"Rentang ditolak: {exc}", file=sys.stderr)
            return 1
    finally:
        db.close()
    print(file=sys.stderr)

    if not hosts:
        print(
            f"Tidak ada host yang menjawab di port {args.port} "
            f"({summary.get('hosts_scanned', 0)} alamat dipindai).\n"
            "Kalau seharusnya ada panel: periksa 'Rute ke ...' di atas — alamat tanpa "
            "rute akan selalu kosong, dan itu bukan tanda panelnya mati.",
            file=sys.stderr,
        )
        return 1

    print(f"{'IP Address':<16} {'Serial Number':<16} {'Device Name':<24} {'Firmware':<20} Status")
    print("-" * 100)
    for hit in hosts:
        status_text = "sudah terdaftar" if hit.get("registered") else "belum terdaftar"
        if hit.get("error"):
            status_text = hit["error"]
        print(
            f"{hit['ip']:<16} {str(hit.get('serial_number') or '-'):<16} "
            f"{str(hit.get('device_name') or '-'):<24} "
            f"{str(hit.get('firmware_version') or '-'):<20} {status_text}"
        )

    print(
        f"\n{len(hosts)} host menjawab dari {summary.get('hosts_scanned', 0)} alamat "
        f"({summary.get('duration_ms', 0) / 1000:.1f} detik)."
    )
    print(
        "Tidak ada data yang ditulis ke panel. Daftarkan device lewat dashboard atau "
        "POST /api/devices."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
