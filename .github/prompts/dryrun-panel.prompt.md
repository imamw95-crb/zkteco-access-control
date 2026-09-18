---
description: "Dry-run push ke satu panel (TIDAK menulis apa pun) dan bandingkan yang diharapkan vs isi panel."
name: "dryrun-panel"
agent: "agent"
argument-hint: "IP panel, mis. 10.100.1.12"
---
Panel target: `${input:panel}`

1. Pastikan device-nya terdaftar (`GET /api/devices` atau `python -m scripts.check_panels --csv ..\devices_input.csv`).
2. Jalankan **dry run**: `POST /api/devices/<id>/personnel/sync?dry_run=true` (untuk smoke test armada: `python -m scripts.live_check --limit 5`).
3. Laporkan: jumlah user di panel vs yang akan ditulis · selisih (+/-) · penyebab paling mungkin.

**Jangan pernah** memakai `dry_run=false`, `scripts.check_push_write`, atau endpoint tulis apa pun
tanpa izin eksplisit user. Kalau panel tidak terbaca, ulangi **sekali** secara sekuensial sebelum
menyimpulkan panel mati (satu panel hanya menerima satu koneksi; kegagalan tunggal ≠ panel mati).
