"""Verify that the write path to a panel actually works.

Run this once against a new panel before trusting pushes to it. It proves the
whole chain — backend, Windows agent, official Pull SDK DLL, panel — with a
single test user, and then puts the panel back exactly as it was.

    python -m scripts.check_push_write 10.100.1.12

SAFETY PROPERTIES (all deliberate):

* writes **only** a `user` row, never `userauthorize`, so the test user has no
  right to open any door even if cleanup were to fail
* no card number, so it cannot collide with a real card
* refuses a panel with more than `MAX_USERS` users, and refuses if the test Pin
  already exists
* captures the full Pin list before and after and diffs it, so "nothing else
  changed" is proven rather than assumed
* stops without deleting anything if the write did not land as expected

This is the only script in `scripts/` that writes to a device.
"""

from __future__ import annotations

import sys

from app.services.push_agent import PushAgentClient

PIN = "TEST-ZK-01"
NAME = "TES PUSH AGENT"
#: Refuse to touch anything big: this is a smoke test, not a rollout tool.
MAX_USERS = 200


def pins(client: PushAgentClient, ip: str) -> list[str]:
    rows = client.read_table(ip, "user", ["Pin"])
    return sorted(r.get("Pin", "") for r in rows)


def main() -> int:
    ip = sys.argv[1] if len(sys.argv) > 1 else ""
    if not ip:
        print("Pakai: python -m scripts.check_push_write <ip-panel>")
        return 2

    client = PushAgentClient()
    if not client.configured:
        print("PUSH_AGENT_URL belum diisi — tidak ada agen untuk diajak bicara.")
        return 2

    print(f"Panel : {ip}")
    print(f"Agent : {client.health()}")

    print("\n[1] Baca kondisi awal")
    before = pins(client, ip)
    print(f"    {len(before)} user di panel")
    if len(before) > MAX_USERS:
        print(f"    BERHENTI: panel ini punya >{MAX_USERS} user, bukan target uji yang aman.")
        return 1
    if PIN in before:
        print(f"    BERHENTI: {PIN} sudah ada di panel. Hapus dulu atau pakai panel lain.")
        return 1

    print(f"\n[2] Tulis satu user uji: Pin={PIN}, Name={NAME} (tanpa kartu, tanpa hak akses)")
    result = client.write_table(ip, "user", [{"Pin": PIN, "Name": NAME}])
    print(f"    agent melaporkan: written={result.get('written')}")
    print(f"    dibaca balik oleh agent: {result.get('verified_records')}")

    print("\n[3] Verifikasi user uji benar-benar ada")
    after_write = pins(client, ip)
    added = sorted(set(after_write) - set(before))
    print(f"    {len(after_write)} user (sebelumnya {len(before)})")
    print(f"    pin baru: {added}")
    if added != [PIN]:
        print("    >>> TIDAK SESUAI HARAPAN. Saya berhenti di sini, tidak menghapus apa pun.")
        print("        Periksa panel sebelum melanjutkan.")
        return 1

    print("\n[4] Hapus user uji")
    client.delete_table(ip, "user", [{"Pin": PIN}])

    print("\n[5] Verifikasi panel kembali seperti semula")
    after_delete = pins(client, ip)
    print(f"    {len(after_delete)} user (awal {len(before)})")
    if after_delete == before:
        print(
            f"\nHASIL: OK. Panel kembali PERSIS seperti semula, {len(before)} user, "
            "daftar pin identik."
        )
        print("       Tulis-hapus ke panel nyata TERBUKTI bekerja.")
        return 0

    removed = sorted(set(before) - set(after_delete))
    left = sorted(set(after_delete) - set(before))
    print(f"    hilang: {removed}")
    print(f"    tersisa: {left}")
    if PIN in after_delete:
        print(f"\nHASIL: GAGAL BERSIH. {PIN} masih ada di panel — hapus manual via ZKAccess.")
        return 1
    print("\nHASIL: daftar pin tidak identik. Periksa panel.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
