#!/usr/bin/env python3
"""
scan_devices.py
================
Script buat narik info & jumlah personnel dari panel ZKTeco C3-100/200/300/400
langsung via protokol Pull SDK (tanpa software ZKAccess/ZKBio), pake library
open source `zkaccess-c3`.

Install dulu:
    pip install zkaccess-c3

Cara pakai:
    1) Dari daftar IP manual (langsung di terminal):
       python3 scan_devices.py --hosts 10.100.1.3 10.100.1.4 10.100.1.5

    2) Dari file CSV berisi daftar device (kolom minimal: name,ip):
       python3 scan_devices.py --csv devices_input.csv

    3) Auto-discover semua device C3 di satu network interface (broadcast):
       python3 scan_devices.py --discover

    Tambahan opsional:
       --password 123456        (kalau device dikasih password koneksi)
       --port 4370              (default 4370, biasanya gak perlu diubah)
       --output hasil.csv       (default: device_report.csv)
       --timeout 4              (detik, default 4)

Output:
    - Ditampilkan sebagai tabel di terminal (mirip kolom di software ZKAccess:
      Device Name, Serial Number, IP Address, Firmware Version, Device Model,
      Personnel Count, Lock Count/Door Count).
    - Disimpan juga ke CSV supaya bisa dibuka di Excel / diimpor ke DB sendiri.
"""

import argparse
import csv
import sys
import time
from dataclasses import dataclass, field

from c3 import C3


@dataclass
class DeviceResult:
    name: str = ""
    ip: str = ""
    serial_number: str = ""
    firmware_version: str = ""
    device_model: str = ""
    lock_count: str = ""
    reader_count: str = ""
    max_user_count: str = ""
    max_finger_count: str = ""
    personnel_count: str = ""
    status: str = "OK"
    error: str = ""


# Param yang mau ditarik dari get_device_param, sesuai kolom di software ZKAccess
DEVICE_PARAMS = [
    "~SerialNumber",
    "~DeviceName",
    "FirmVer",
    "LockCount",
    "ReaderCount",
    "~MaxUserCount",
    "~MaxUserFingerCount",
]


def guess_device_model(lock_count: str, reader_count: str) -> str:
    """
    Device model (C3-100/200/400) gak selalu dikembalikan langsung sebagai
    string oleh param FirmVer/MachineType. Pendekatan paling stabil: tebak
    dari jumlah pintu (LockCount) yang didukung panel.
    Kalau device lo C3-300 (4 pintu, RS485 based), tetap masuk ke C3-400 di
    sisi jumlah-pintu (sama-sama support 4 pintu) -- cukup buat keperluan
    inventaris, sesuaikan manual kalau perlu presisi model persis.
    """
    try:
        n = int(lock_count)
    except (TypeError, ValueError):
        return "Unknown"
    return {1: "C3-100", 2: "C3-200", 4: "C3-400 / C3-300"}.get(n, f"Unknown ({n} pintu)")


def count_personnel(panel: C3) -> int:
    """
    Hitung jumlah user (personnel) yang tersimpan di device dengan menarik
    tabel 'user' langsung dari panel (bukan dari MaxUserCount, yang cuma
    kapasitas maksimum, bukan jumlah user aktual).
    """
    records = panel.get_device_data("user")
    return len(records)


def scan_one(name: str, ip: str, port: int, password: str, timeout: int) -> DeviceResult:
    result = DeviceResult(name=name, ip=ip)
    panel = C3(ip, port)
    panel.receive_timeout = timeout

    try:
        connected = panel.connect(password) if password else panel.connect()
        if not connected:
            result.status = "GAGAL"
            result.error = "Tidak bisa connect (cek IP/port/network/firewall)"
            return result

        params = panel.get_device_param(DEVICE_PARAMS)

        result.serial_number = params.get("~SerialNumber", "")
        # Kalau device kasih nama sendiri, override nama dari CSV/input
        result.name = params.get("~DeviceName") or name
        result.firmware_version = params.get("FirmVer", "")
        result.lock_count = params.get("LockCount", "")
        result.reader_count = params.get("ReaderCount", "")
        result.max_user_count = params.get("~MaxUserCount", "")
        result.max_finger_count = params.get("~MaxUserFingerCount", "")
        result.device_model = guess_device_model(result.lock_count, result.reader_count)

        try:
            result.personnel_count = str(count_personnel(panel))
        except Exception as e:  # tabel user kadang gak didukung semua firmware
            result.personnel_count = "N/A"
            result.error = f"(personnel count gagal: {e})"

    except Exception as e:
        result.status = "GAGAL"
        result.error = str(e)
    finally:
        try:
            panel.disconnect()
        except Exception:
            pass

    return result


def load_hosts_from_csv(path: str) -> list[tuple[str, str]]:
    hosts = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("name") or row.get("Device Name") or ""
            ip = row.get("ip") or row.get("IP Address") or ""
            if ip:
                hosts.append((name.strip(), ip.strip()))
    return hosts


def discover_devices(timeout: int) -> list[tuple[str, str]]:
    print("Mencari device C3 di network lewat broadcast discovery...")
    found = C3.discover(timeout=timeout)
    hosts = []
    for dev in found:
        hosts.append((getattr(dev, "device_name", "") or "", dev.host))
    return hosts


def print_table(results: list[DeviceResult]) -> None:
    headers = [
        "Device Name", "IP Address", "Serial Number", "Firmware Version",
        "Device Model", "Personnel", "Lock/Door", "Status",
    ]
    rows = [
        [
            r.name, r.ip, r.serial_number, r.firmware_version,
            r.device_model, r.personnel_count, r.lock_count,
            r.status if r.status == "OK" else f"{r.status} - {r.error}",
        ]
        for r in results
    ]
    widths = [max(len(str(h)), *(len(str(row[i])) for row in rows)) if rows else len(h)
              for i, h in enumerate(headers)]

    def fmt_row(row):
        return " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(row))

    print(fmt_row(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def save_csv(results: list[DeviceResult], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Device Name", "IP Address", "Serial Number", "Firmware Version",
            "Device Model", "Personnel Count", "Lock Count", "Reader Count",
            "Max User Count", "Max Finger Count", "Status", "Error",
        ])
        for r in results:
            writer.writerow([
                r.name, r.ip, r.serial_number, r.firmware_version,
                r.device_model, r.personnel_count, r.lock_count, r.reader_count,
                r.max_user_count, r.max_finger_count, r.status, r.error,
            ])
    print(f"\nHasil lengkap disimpan ke: {path}")


def main():
    parser = argparse.ArgumentParser(description="Scan device ZKTeco C3 langsung via Pull SDK protocol")
    parser.add_argument("--hosts", nargs="+", help="Daftar IP device, pisah spasi")
    parser.add_argument("--csv", help="Path file CSV input berisi kolom name,ip")
    parser.add_argument("--discover", action="store_true", help="Auto-discover device via broadcast")
    parser.add_argument("--password", default=None, help="Password koneksi device (kalau ada)")
    parser.add_argument("--port", type=int, default=4370, help="Port TCP device (default 4370)")
    parser.add_argument("--timeout", type=int, default=4, help="Timeout koneksi per device (detik)")
    parser.add_argument("--output", default="device_report.csv", help="Path file CSV output")
    args = parser.parse_args()

    hosts: list[tuple[str, str]] = []
    if args.discover:
        hosts = discover_devices(args.timeout)
    elif args.csv:
        hosts = load_hosts_from_csv(args.csv)
    elif args.hosts:
        hosts = [("", ip) for ip in args.hosts]
    else:
        print("Wajib isi salah satu: --hosts, --csv, atau --discover")
        sys.exit(1)

    if not hosts:
        print("Gak ada device ditemukan / terdaftar.")
        sys.exit(1)

    print(f"Scan {len(hosts)} device...\n")
    results = []
    for name, ip in hosts:
        print(f"-> Connecting ke {name or '(no name)'} [{ip}] ...", end=" ", flush=True)
        t0 = time.time()
        r = scan_one(name, ip, args.port, args.password, args.timeout)
        dt = time.time() - t0
        print(f"{r.status} ({dt:.1f}s)")
        results.append(r)

    print()
    print_table(results)
    save_csv(results, args.output)


if __name__ == "__main__":
    main()
