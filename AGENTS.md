# AGENTS.md — ZKTeco C3 Access Control ("pengganti ZKAccess 3.5")

> Kontrak untuk AI coding agent. **File ini SELALU dimuat, karena itu sengaja pendek.**
> Detail dipindah ke `.github/instructions/*.instructions.md` yang hanya dimuat saat relevan
> (§5) — **jangan menyalin isinya kembali ke sini.**
> Kalau ada konflik antar dokumen, **file ini yang benar** (klaim usang: §6).

Alembic head: `a1d4c7b90e35`. Angka yang cepat basi (jumlah tes, jumlah baris DB) **sengaja
tidak ditulis** — jalankan perintahnya, jangan percaya angka di dokumen.

## 0. Disiplin token (berlaku di setiap permintaan)

- **Cari dulu, baca kemudian.** `grep_search` nama simbol → `read_file` hanya pada rentang baris. Jangan membaca satu file utuh untuk satu simbol.
- Jangan membaca ulang file yang sudah dibaca di sesi ini.
- Satu `read_file` rentang besar lebih murah daripada banyak panggilan kecil; panggilan yang tidak saling bergantung dikirim dalam SATU pesan.
- Eksplorasi luas ("di mana X dipakai?", "bagaimana Y bekerja?") → subagent **Explore**; ia mengembalikan ringkasan, bukan isi file ke chat ini.
- Jangan menempel blok kode yang tidak berubah, ringkasan isi file, atau pengulangan rencana. Rujuk `path:line`.
- Jalankan pytest/ruff **hanya** kalau kode berubah; kalau tidak, pakai hasil terakhir.
- `zkteco-backend/README.md` (823 baris) dan `docs/*.md` dibuka hanya kalau §5 atau instruksi on-demand menyuruh.

## 0b. Shortcut chat (pakai ini, jangan menjelaskan ulang)

`/cek` pytest + ruff · `/jalankan-backend` nyalakan backend · `/dryrun-panel <ip>` dry-run push (tanpa tulis) · `/cari <pertanyaan>` eksplorasi murah.

---

## 1. Isi workspace (dua hal berbeda — jangan tertukar)

- `zkteco-backend/` = **produk sebenarnya**: FastAPI + SQLAlchemy 2.0 + Alembic + APScheduler, pengganti ZKAccess 3.5 untuk 23 panel ZKTeco C3 (`10.100.1.x`, TCP 4370). Target kerja normal.
- `zkteco-backend/agent/` = **Windows push agent** (stdlib-only, Python **32-bit**): satu-satunya jalur **menulis** ke panel.
- `zkteco-backend/live_check.db` = DB dev nyata (jangan hapus). `test_zkteco.db`, `scan_devices.py`, `devices_input.csv`, `device_report.csv`, `*.xls`, `backup new.bak` = artefak recon, **bukan** aplikasi → jangan dihapus, jangan "dirapikan".
- `.venv/` (root) dan `zkteco-backend/.venv/` = **dua venv berbeda**; yang benar `zkteco-backend\.venv` (Python 64-bit; venv root tidak punya `uvicorn`/`pytest`).
- `jalankan-backend.bat` / `hentikan-backend.bat` / `status-backend.bat` + `.vscode/tasks.json` = tombol dev (proses **latar** + supervisor auto-restart, log di `zkteco-backend/logs/`).
- Tidak ada `.git` di workspace ini — jangan asumsikan perintah git berhasil.

---

## 2. Perintah (path absolut, salin apa adanya)

```powershell
$PY = "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe"
& $PY -m pytest -q --rootdir "C:\laragon\www\zkteco\zkteco-backend" "C:\laragon\www\zkteco\zkteco-backend\tests"
& $PY -m ruff check "C:\laragon\www\zkteco\zkteco-backend"
& $PY -m ruff format --check "C:\laragon\www\zkteco\zkteco-backend"
& $PY -m alembic heads        # dari zkteco-backend/ ; harus tetap a1d4c7b90e35
```

- Deploy ulang ke server Docker (`192.168.0.27`) = satu perintah:
  `powershell -ExecutionPolicy Bypass -File C:\laragon\www\zkteco\zkteco-backend\deploy\push_to_server.ps1`
  → detail + jebakan: `deploy-server.instructions.md` (§5).

- Backend lokal (tombol dev / manual + env), semua skrip operasional (`live_check`, `boot_check`, `check_panels`, `scan_range`, `verify_person_on_panels`, `check_push_write`, `make_bundle`), dan jebakan host Windows → `.github/instructions/dev-commands.instructions.md`.
- **Selalu path absolut**: tool terminal sering membuang `Set-Location`, sehingga `.\.venv\Scripts\python.exe` bisa resolve ke venv yang salah.
- `PUSH_AGENT_URL` harus ada di env proses **server**; kalau kosong push menjawab **503** (`dry_run` tetap jalan tanpa agent).

---

## 3. Aturan emas (melanggar = regresi)

Detail + alasan tiap aturan ada di instruksi on-demand (§5) — ringkasan ini wajib dibaca selalu.

1. Route `app/api/*` **tidak pernah** `import c3`; alur wajib router → service → `DeviceClient`.
2. Tes tidak boleh butuh hardware; device palsu lewat `set_client_factory(FakeDeviceClient)`.
3. Baca = `zkaccess-c3` in-process; tulis = HTTP ke agent Windows. **Jangan pernah memuat `plcommpro.dll` di backend** (32-bit vs 64-bit).
4. Sumber kebenaran hak akses: `personnel_access_groups` + `access_group_doors`. `Personnel.access_group_id` / `AccessGroup.door_numbers` = tampilan saja.
5. `SetDeviceData` **mengganti** record: field yang tidak dikirim jadi kosong → `panel_push._carry_over()` wajib tetap ada.
6. Push **diff-based**: panel dibaca dulu; tidak terbaca = **tidak menulis apa pun**.
7. Baris `user` **tidak pernah dihapus**; pencabutan = hapus `userauthorize`, disapu ke **semua** device. Push `personnel_ids` = **otoritatif**; push se-panel = **aditif**.
8. Dashboard: **jangan** `confirm()` bawaan browser — pakai `askConfirm()`. Jangan memakai klik Playwright berbasis koordinat.
9. Jangan menulis ke panel nyata tanpa izin user: `dry_run` → lapor → izin → satu panel uji → verifikasi → baru meluas.
10. Alembic = sumber kebenaran skema (`create_all` hanya untuk DB baru); jangan edit revisi lama; satu head.
11. ZKAccess SQL Server tetap otoritas fallback (read-only, kredensial dipegang user).
12. Panel menerima **satu** koneksi sekaligus; kegagalan tunggal ≠ panel mati (ulangi sekuensial). Service API memakai `SCHEDULER_ENABLED=false`.
13. Menyimpan personel di dashboard **juga menulis ke panel** (default aktif, boleh dimatikan). Hasil push dilaporkan **per panel** — jangan pernah bilang "selesai" saja: panel yang gagal **masih memegang data lama**.
14. Mengubah access level **tidak** menulis ke panel → ketiga aksi tab Access Level wajib menawarkan kirim dan bilang "PANEL BELUM DIUBAH" kalau ditolak/gagal.
15. Menulis alamat jaringan panel (`POST /api/devices/{id}/network`) **default OFF** (`PANEL_NETWORK_WRITE_ENABLED`), dan tetap butuh dry run dengan nilai yang sama.
16. Login wajib untuk **semua** `/api` (kecuali `/api/auth/login|logout`). Dua role: `admin` (semua) dan `hr` (personel + kirim ke panel, departemen, access level). Guard ditegakkan **di server** (`app/api/deps.py`); menyembunyikan tab cuma kenyamanan. Endpoint baru = daftarkan di `app/api/__init__.py`, dan sweep perilaku di `tests/test_auth.py` akan menolak kalau lupa.

---

## 4. Arsitektur ringkas

```
app/api/*  →  app/services/*  →  device_client.DeviceClient  →  zkaccess-c3      (BACA)
                              →  panel_push → push_agent → agent Windows → plcommpro.dll (TULIS)
```

- `device_client.py` = **satu-satunya** modul yang menyentuh `zkaccess-c3`; `c3_compat.py` = tambalan bug library/firmware.
- `sync_service.py` job armada (thread per device + session DB sendiri) · `panel_push.py` menurunkan record panel tanpa I/O · `push_agent.py` klien HTTP agent · `network_scan.py` cari panel per rentang IP (read-only) · `panel_network.py` alamat panel (tulis default OFF) · `monitor_service.py` + `api/monitor.py` feed monitoring **baca DB saja, tidak pernah menyentuh panel** · `zkaccess_source.py` pembaca SQL Server ZKAccess (SELECT/WITH saja).
- `app/static/dashboard.html` = seluruh UI (satu file, vanilla JS, tanpa build step). Lainnya: `models.py` `schemas.py` `config.py` `database.py` `main.py`, `workers/` APScheduler, `migrations/` Alembic, `tests/` pytest, `scripts/` operasional, `deploy/` deploy, `docs/` dokumen.
- Pemetaan tabel panel ↔ model (terminasuk bitmask pintu) → `.github/instructions/panel-write.instructions.md`.

---

## 5. Instruksi on-demand (`.github/instructions/`) — dimuat otomatis saat relevan

**Jangan menyalin isi file-file ini ke sini.** Baca file yang cocok sebelum mengubah area itu.

| File | Dimuat saat |
|---|---|
| `panel-write.instructions.md` | push/sync ke panel, agent Windows, `userauthorize`, `dry_run`, alamat jaringan panel — **fakta hardware terverifikasi, jebakan `-201`, jebakan "kartu tidak membuka pintu"** |
| `dashboard-ui.instructions.md` | menyentuh `app/static/dashboard.html`, tombol/form/modal/tab, menguji UI dengan Playwright — **dua insiden yang pernah merusak data** |
| `migrations.instructions.md` | mengubah skema atau revisi Alembic |
| `tests.instructions.md` | menulis atau menjalankan pytest |
| `dev-commands.instructions.md` | menjalankan backend, skrip operasional, menulis `.ps1`/`.bat` di host Windows ini |
| `deploy-server.instructions.md` | deploy/deploy ulang ke Docker di `192.168.0.27`, menyalin data ke Postgres server, menyalakan worker, atau mengubah `deploy/*` |
| `zkaccess-and-data.instructions.md` | membandingkan dengan ZKAccess, menjalankan skrip migrasi, butuh angka data terkini |

---

## 6. Klaim usang — jangan disebarkan

- "Push ke panel tidak mungkin / endpoint sync membalas **501**" → **salah**. Push jalan lewat agent Windows; **503** hanya berarti `PUSH_AGENT_URL` belum diisi.
- "Kontrol pintu tidak bisa dilakukan library ini" → **bisa** (perintah CONTROL, bukan SETDATA).
- "Ubah IP panel tidak ada" → **ada** (`POST /api/devices/{id}/network`), tapi **default OFF** (`PANEL_NETWORK_WRITE_ENABLED`); dry run tetap boleh walau tulis belum aktif.
- "Pencarian device hanya lewat UDP broadcast" → ada **tab Cari Device** (`POST /api/scan`, `python -m scripts.scan_range`) untuk rentang IP; batasnya `SCAN_ALLOWED_NETWORKS`, identifikasinya berurutan (satu koneksi per panel) dan hanya membaca.
- Angka tes apa pun di dokumen ("191", "247", "273", …) adalah snapshot → jalankan pytest.
- "Alembic head = `907835756e6e`" → head sekarang **`a1d4c7b90e35`**.
- `docs/DEVICE_PROTOCOL_NOTES.md` **§1 sudah usang** (lihat banner di file itu); sisanya berlaku.

## 7. Alur kerja

1. Cari dulu nama simbolnya (`grep`) — fiturnya sering sudah ada.
2. Ubah kode **+ tambahkan tes** di `tests/` yang mengunci perilakunya.
3. Jalankan pytest + ruff (§2) sampai keduanya bersih sebelum melapor.
4. Menyentuh skema → buat revisi Alembic baru; `alembic heads` harus tetap satu head.
5. Menyentuh panel → `dry_run` → lapor → **izin user** → satu panel uji → verifikasi (`scripts/verify_person_on_panels.py`) → baru meluas.
6. Perbarui dokumen yang perilakunya berubah **di commit yang sama** — dokumen yang salah lebih berbahaya daripada tidak ada dokumen.

## 8. Peta dokumen

| Dokumen | Isi | Catatan |
|---|---|---|
| `AGENTS.md` (file ini) | Kontrak AI agent, selalu dimuat | **Tertinggi** kalau ada konflik |
| `.github/instructions/*` | Detail on-demand (§5) | Dimuat otomatis saat relevan |
| `.github/prompts/*` | Shortcut chat (§0b) | `/cek`, `/cari`, `/jalankan-backend`, `/dryrun-panel` |
| `zkteco-backend/README.md` | Gambaran produk, cara jalan, status fitur | Akurat — 823 baris, buka seperlunya |
| `zkteco-backend/agent/README.md` | Deploy agent Windows (Python 32-bit, DLL, Task Scheduler) | Rujukan utama soal DLL |
| `zkteco-backend/docs/INSTALL.md` | Instalasi (dev Windows, Linux, Docker, agent, migrasi) + troubleshooting | Rujukan utama cara memasang |
| `zkteco-backend/docs/TUTORIAL.md` | Tutorial dashboard per tab, resep harian, diagnosis pintu | Rujukan utama untuk operator |
| `zkteco-backend/docs/DEPLOYMENT.md` | Runbook deploy ke `192.168.0.27` | Belum dieksekusi (butuh password SSH diisi manusia) |
| `zkteco-backend/docs/DEVICE_PROTOCOL_NOTES.md` | Temuan protokol firmware | **§1 usang**, sisanya berlaku |
