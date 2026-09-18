---
description: "Use when deploying or updating the backend on the Docker host 192.168.0.27: push_to_server.ps1, deploy/server_*.sh, docker-compose.yml, Dockerfile, copying data from the laptop's SQLite into the server's PostgreSQL, or enabling the worker there. Berisi satu perintah deploy ulang dan jebakan yang sudah memakan waktu."
applyTo: ["**/deploy/**", "**/docker-compose.yml", "**/Dockerfile", "**/.env.example", "**/scripts/copy_sqlite_to_postgres.py", "**/tests/test_deploy_scripts.py"]
---
# Deploy ke server Docker (192.168.0.27)

Target: `sentral@192.168.0.27`, kode di **`/home/sentral/zkteco-backend`** (bukan `/opt` —
itu butuh sudo). SSH keyless lewat alias `zkteco-prod`. Prosedur panjang/manual ada di
`zkteco-backend/docs/DEPLOYMENT.md`; **file ini yang harus dibaca sebelum mengubah apa pun
di `deploy/`**.

## Deploy ulang setelah update fitur (satu perintah)

```powershell
powershell -ExecutionPolicy Bypass -File C:\laragon\www\zkteco\zkteco-backend\deploy\push_to_server.ps1
```

Tiga langkah di dalamnya:

1. bungkus kode jadi `zkteco-src.tar.gz` — **tanpa `.env`, tanpa `*.db`**;
2. `scp` ke `zkteco-prod:/tmp/zkteco-src.tar.gz`;
3. `ssh`: hapus+pasang ulang `app/ migrations/ scripts/`, `chmod +x deploy/*.sh`, lalu
   `bash deploy/server_deploy.sh` → `docker compose up -d --build db backend` → tunggu
   `/health` → verifikasi.

Saklar: `-UploadOnly` (kirim kode, jangan nyalakan ulang) dan `-ForceCopyData`
(timpa database server dengan `live_check.db` — **hati-hati**, data server diganti).

## Yang tidak pernah tersentuh saat deploy ulang

| Hal | Kenapa |
|---|---|
| `.env` server | tidak ikut di dalam arsip, jadi tidak mungkin tertimpa |
| `live_check.db` di server | sama, tidak ikut diarsipkan |
| data di volume `zkteco_pgdata` | skrip tidak pernah menyentuh volume; `docker compose down` pun tidak menghapusnya (hanya `down -v`) |
| penyalinan data ulang | **otomatis dilewati** — `server_deploy.sh` menolak menimpa begitu `devices + personnel + sync_runs > 0` |

## Sebelum mengirim (aturan repo, bukan saran)

- `pytest` + `ruff check` + `ruff format --check` bersih (§2 `AGENTS.md`).
- Skema berubah → buat **revisi Alembic baru** (jangan edit yang lama) dan `alembic heads`
  tetap **satu head**. Migrasi dijalankan otomatis oleh container `backend` saat start
  (`alembic upgrade head`), jadi tidak ada langkah tambahan saat deploy.
- Menambah **skrip server baru** di `deploy/` → tambahkan namanya ke daftar di
  `push_to_server.ps1`, kalau tidak skrip itu tidak ikut terkirim.

## Menyalin data laptop → PostgreSQL server

`deploy/server_deploy.sh` melakukannya sendiri, dengan aturan:

- `devices + personnel + sync_runs = 0` di server → hanya data awal aplikasi
  (`access_time_zones "24 Jam"` + akun `admin`/`hr` dari seeding) → aman dikosongkan lalu diisi;
- sudah ada data sungguhan → **dilewati** supaya tidak ada yang tertimpa;
- `--force-copy` (atau `-ForceCopyData`) untuk memaksa, mis. setelah pindah data resmi.

Skrip penyalin: `scripts/copy_sqlite_to_postgres.py`
(`--source <file sqlite> [--dry-run] [--force]`). Sumber dibuka **read-only**
(`?mode=ro`), `auth_sessions` sengaja tidak disalin, dan `id` disalin apa adanya
dengan `setval` supaya sequence Postgres tidak menabrak id berikutnya.
Tesnya: `tests/test_copy_sqlite_to_postgres.py`.

## Worker: jangan dinyalakan selama laptop masih hidup

Panel C3 hanya menerima **satu** koneksi sekaligus. Backend dev di laptop masih menarik log
panel yang sama, jadi `worker` (penjadwal log + health check) dibiarkan mati di server.
Nyalakan **setelah** backend laptop dihentikan:

```powershell
ssh zkteco-prod "cd /home/sentral/zkteco-backend && docker compose up -d worker"
```

`tests/test_deploy_scripts.py::test_server_deploy_leaves_the_worker_off` mengunci ini.

## Fakta host yang sudah diverifikasi (jangan menebak lagi)

- Docker **sudah terpasang** (29.4.3 + compose v5.1.3). Port terpakai: 80, 443, 888, 9090,
  3000, 8080, 8081, 3306, 21, 22, 18789/18790 → **hanya 8000 yang dipakai project ini**;
  5432 tetap di dalam network compose.
- Di host itu sudah ada project compose lain (network `172.18`/`172.19`) → `docker-compose.yml`
  memakai `name: zkteco` supaya network/volume tidak bertabrakan.
- `sentral` sudah masuk grup **docker** (2026-09-18) → perintah docker jalan tanpa sudo.
  `sudo` sendiri masih **minta sandi**, dan sandi itu harus diketik manusia — jangan pernah
  mengirimkannya lewat perintah/`vscode_askQuestions`.
- Routing ke panel dari server: `10.100.1.x:4370` **terbuka** (diuji .3/.12/.21), dan agen push
  `10.100.1.100:8081` menjawab `/health` **200** dengan token yang dipakai backend.
- Postgres server: `select` cepat lewat
  `docker compose exec -T db psql -U zkteco -d zkteco -c "..."`.
  Ringkasan status: `bash deploy/server_status.sh` (nilai rahasia disamarkan).

## Jebakan yang sudah memakan waktu

- **`docker-compose.yml` wajib punya `env_file: .env` di `backend` dan `worker`.** Tanpa itu
  container tidak pernah melihat `PUSH_AGENT_URL`/`PUSH_AGENT_TOKEN` dan **setiap push menjawab 503**.
  (systemd memakai `EnvironmentFile=.env`, jadi ini menyamakan perilakunya.)
- **Tabel tujuan tidak pernah kosong**: aplikasi menyemai time zone + akun saat start pertama.
  Karena itu penyalin menolak tanpa `--force` — dan itu **perilaku yang benar** (kegagalan yang
  aman; seluruh transaksi di-rollback, bukan setengah terisi).
- **`.sh` wajib LF.** `\r` di akhir baris membuat bash membaca `set -eu\r` sebagai nama perintah.
  `tests/test_deploy_scripts.py` memeriksa setiap berkas `.sh` di `deploy/`.
- **`.ps1` wajib ASCII tanpa BOM** — PowerShell 5.1 membacanya sebagai ANSI.
- **PowerShell 5.1 tidak punya `&&`**; dan `scp` menulis progress ke stderr → dengan
  `$ErrorActionPreference='Stop'` itu jadi *terminating error* yang mematikan skrip di tengah.
  Karena itu `push_to_server.ps1` memakai `Invoke-Native` (EA `Continue` + periksa `$LASTEXITCODE`).
- **Heredoc `<<EOF` tanpa kutip di skrip server**: backtick di dalamnya (walau hanya di komentar)
  dijalankan sebagai command substitution → `line NN: secure: command not found` + berkas rusak.
- **PowerShell → ssh mengubah `\$`**: `sed -i '/^KEY=$/d'` sampai di server sebagai `\$` dan
  mencocokkan `$` literal, jadi tidak menghapus apa pun. Pakai `awk`/tulis ulang berkas.
- **Baris terakhir `.env` harus diakhiri newline.** Token yang ditempel lewat pipe tanpa
  `printf '\n'` membuat `while read` melewatkan baris terakhir.
- `grep -vE '^[0-9a-f]{12} (Pulling|Download|...)'` dipakai untuk menyaring riuh-nya build
  supaya keluarannya bisa dibaca.

## Kalau ada yang gagal

1. `bash deploy/server_status.sh` — `.env` (disamarkan), agen push, `docker compose ps`.
2. `docker compose logs --tail 60 backend` (atau `db`) di `/home/sentral/zkteco-backend`.
3. `curl -s localhost:8000/health` di server; dari laptop: `http://192.168.0.27:8000/health`.
4. Kalau `/health` hidup tapi semua panel "offline": itu routing/firewall ke `10.100.1.0/24`,
   bukan aplikasi — bandingkan dengan `python -m scripts.check_panels`.

## Yang belum pernah dilakukan (jangan diklaim sudah)

Belum ada satu pun **penulisan ke panel** dari server itu, dan `worker` belum pernah dinyalakan.
Push pertama dari sana tetap harus lewat gerbang biasa: `dry_run` → lapor → **izin user** →
satu panel uji → verifikasi (`scripts/verify_person_on_panels.py`) → baru meluas.
