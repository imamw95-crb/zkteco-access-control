"""Pre-flight check: can this host reach the access control panels?

Run this **on the host that will run the backend** before deploying. The app is
useless if the host cannot open a TCP connection to port 4370 on the panels.

    python -m scripts.check_panels --csv ../devices_input.csv
    python -m scripts.check_panels --hosts 10.100.1.3 10.100.1.14

Exit codes:
    0  at least one panel reachable (partial failures are normal)
    1  no panel reachable, or the CSV could not be read
"""

from __future__ import annotations

import argparse
import csv
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_PORT = 4370


def load_targets(csv_path: Path | None, hosts: list[str], port: int) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = [(h, h) for h in hosts]
    if csv_path is None:
        return targets

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            ip = row.get("ip") or row.get("ip address") or ""
            if ip:
                targets.append((row.get("name") or ip, ip))
    return targets


def probe(ip: str, port: int, timeout: float) -> tuple[bool, float | None, str | None]:
    import time

    started = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, (time.perf_counter() - started) * 1000, None
    except TimeoutError:
        return False, None, "timeout (kemungkinan diblokir firewall)"
    except ConnectionRefusedError:
        return False, None, "connection refused (host ada, port 4370 tertutup)"
    except OSError as exc:
        return False, None, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--hosts", nargs="*", default=[])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args(argv)

    if args.csv and not args.csv.exists():
        print(f"CSV tidak ditemukan: {args.csv}", file=sys.stderr)
        return 1

    targets = load_targets(args.csv, args.hosts, args.port)
    if not targets:
        print("Tidak ada target. Pakai --csv atau --hosts.", file=sys.stderr)
        return 1

    host = socket.gethostname()
    print(f"Host      : {host}")
    print(f"Target    : {len(targets)} panel, TCP port {args.port}, timeout {args.timeout}s")
    print(f"Local IPs : {', '.join(_local_ips()) or 'n/a'}")
    print()

    results: list[tuple[str, str, bool, float | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(probe, ip, args.port, args.timeout): (name, ip) for name, ip in targets
        }
        for future, (name, ip) in futures.items():
            ok, latency, error = future.result()
            results.append((name, ip, ok, latency, error))

    results.sort(key=lambda r: (not r[2], r[1]))

    print(f"{'Device Name':<28} {'IP Address':<15} {'Result':<7} {'Latency':>9}  Detail")
    print("-" * 92)
    for name, ip, ok, latency, error in results:
        latency_text = f"{latency:.0f} ms" if latency is not None else "-"
        print(f"{name:<28} {ip:<15} {'OK' if ok else 'GAGAL':<7} {latency_text:>9}  {error or ''}")

    reachable = sum(1 for r in results if r[2])
    print(f"\n{reachable}/{len(results)} panel bisa dijangkau dari host ini.")

    if reachable == 0:
        print(
            "\nKESIMPULAN: host ini TIDAK punya rute ke jaringan panel.\n"
            "Deploy backend di sini tidak akan berfungsi. Pilihan:\n"
            "  1. Jalankan backend di host yang bisa menjangkau 10.100.1.x\n"
            "  2. Tambahkan route/firewall rule ke subnet panel\n"
            "  3. Pastikan kebijakan firewall mengizinkan TCP 4370",
            file=sys.stderr,
        )
        return 1

    if reachable < len(results):
        print(
            f"\nCATATAN: {len(results) - reachable} panel tidak terjangkau. "
            "Device akan tampil 'offline' di dashboard; job sync tetap jalan "
            "untuk panel lain (kegagalan diisolasi per-device)."
        )
    else:
        print("\nSemua panel terjangkau. Siap di-deploy.")

    return 0


def _local_ips() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidate = info[4][0]
            if not candidate.startswith("127.") and candidate not in ips:
                ips.append(candidate)
    except OSError:
        pass
    return ips


if __name__ == "__main__":
    raise SystemExit(main())
