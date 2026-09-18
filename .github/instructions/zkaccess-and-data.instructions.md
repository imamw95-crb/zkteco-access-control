---
description: "Use when comparing against the ZKAccess SQL Server or legacy ZKAccess data, running the migration scripts, or when you need current row counts / the state of the live development database."
applyTo: ["**/app/services/zkaccess_source.py", "**/scripts/*zkaccess*", "**/scripts/verify_migration.py"]
---
# ZKAccess (fallback otoritatif) & kondisi data

## ZKAccess SQL Server tetap otoritas fallback
Kalau data di aplikasi ini mencurigakan, **baca ZKAccess read-only untuk membandingkan**.
Terbukti menyelamatkan data: nilai `id 266` yang tertimpa dipulihkan dari sana
(`USERINFO.CardNo`, `acc_levelset_emp` + `acc_levelset`, `DEPARTMENTS`).
- `ZKAccess` (~712 MB) di `10.100.1.100\SQLEXPRESS`, SQL Server 2022 Express. Kredensial dipegang **user** dan **tidak** ditulis di kode (env `ZKACCESS_SERVER` / `ZKACCESS_USER` / `ZKACCESS_PASSWORD`).
- Konektor `app/services/zkaccess_source.py` **menolak** apa pun selain `SELECT`/`WITH` (DELETE ditolak di level kode). Jangan melonggarkan ini.
- Skrip: `scripts/check_zkaccess_source.py`, `scripts/migrate_from_zkaccess.py` (`--dry-run`), `scripts/verify_migration.py`. Butuh `pyodbc` + ODBC Driver 11.
- Referensi tabel: `USERINFO` 528 · `Machines` 23 · `acc_levelset` 12 · `acc_levelset_emp` 2037 · `acc_levelset_door_group` 87 · `acc_monitor_log` ~2,09 juta · `DEPARTMENTS` 28 · `acc_door` 27.
- Log akses **sengaja tidak dimigrasikan** (`acc_monitor_log`, `DEVICE_PARAMS`).
- `USERINFO.Badgenumber` = PIN yang dipakai panel; `USERID` hanya surrogate internal (dipakai `acc_levelset_emp.employee_id`).
- `Machines.usercount` cocok **persis** dengan bacaan Pull-SDK (525/387/523/27/522) — verifikasi silang bahwa kedua sumber benar.
- `acc_monitor_log`: `event_type=300` (mayoritas) = status pintu tanpa pin; `event_type=1000` = akses kartu nyata, semua punya pin (filter `pin<>''`).

## Snapshot data dev (2026-09-17 — VOLATILE)
`live_check.db`: 23 device · 531 personel · 12 access level · 87 level→pintu ·
2.040 keanggotaan orang↔level · 28 departemen (root "RSMP PATROL") · 1 zona waktu
(slot 1 = "24 Jam", dipakai **semua** level) · ~1.063 baris log akses.
Angka ini bergerak — kalau keputusan Anda bergantung padanya, **query dulu**, jangan percaya snapshot.

## Aturan yang tetap berlaku
- Jangan menjalankan migrasi atau push terhadap data nyata tanpa izin user (`dry_run` dulu — lihat `panel-write.instructions.md`).
- Artefak recon di root (`scan_devices.py`, `devices_input.csv`, `device_report.csv`, `*.xls`, `backup new.bak`, `test_zkteco.db`) **bukan** bagian aplikasi: jangan dihapus, jangan "dirapikan".
