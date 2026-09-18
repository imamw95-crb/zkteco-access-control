# ZKTeco C3 Access Control

Pengganti ZKAccess 3.5 untuk armada panel **ZKTeco C3** (23 panel, `10.100.1.x`, TCP 4370):
FastAPI + SQLAlchemy 2.0 + Alembic + APScheduler, dashboard satu berkas (vanilla JS, tanpa
build step), dan **Windows push agent** (Python 32-bit) sebagai satu-satunya jalur **menulis**
ke panel.

> Repo ini **privat** dan berisi detail infrastruktur (IP panel, nama ruangan). Jangan dijadikan
> publik, dan jangan pernah `git add -f` berkas data/rahasia yang sudah diabaikan.

## Isi

| Path | Isi |
|---|---|
| `AGENTS.md` | kontrak kerja untuk AI agent — **baca ini dulu** |
| `.github/instructions/` | aturan rinci per area (panel, dashboard, migrasi, tes, deploy, data) |
| `zkteco-backend/app/` | API FastAPI, service, model, dashboard (`static/dashboard.html`) |
| `zkteco-backend/agent/` | push agent Windows + installer Inno Setup |
| `zkteco-backend/migrations/` | revisi Alembic (satu head: `a1d4c7b90e35`) |
| `zkteco-backend/scripts/` | skrip operasional (cek panel, tarik log, salin data, verifikasi) |
| `zkteco-backend/deploy/` | launcher backend lokal + skrip deploy ke server Docker |
| `zkteco-backend/tests/` | pytest (tanpa hardware; device palsu lewat `set_client_factory`) |
| `zkteco-backend/docs/` | INSTALL, TUTORIAL, DEPLOYMENT, catatan protokol firmware |

## Mulai cepat

```powershell
# backend lokal (proses latar + supervisor auto-restart)
.\jalankan-backend.bat          # Ctrl+Shift+B

# pytest + ruff (path absolut — venv root tidak punya pytest)
$PY = "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe"
& $PY -m pytest -q --rootdir "C:\laragon\www\zkteco\zkteco-backend" "C:\laragon\www\zkteco\zkteco-backend\tests"

# deploy ulang ke server Docker
powershell -ExecutionPolicy Bypass -File .\zkteco-backend\deploy\push_to_server.ps1
```

## Aturan yang tidak boleh dilanggar

Lihat `AGENTS.md` §3 (aturan emas) — ringkasnya: route API tidak pernah `import c3`; tulis ke
panel hanya lewat agent Windows; sumber kebenaran hak akses adalah `personnel_access_groups`
(+ `access_group_doors`); push bersifat diff-based dan **tidak menulis apa pun kalau panel tidak
terbaca**; baris `user` tidak pernah dihapus (pencabutan = hapus `userauthorize`); dan menulis ke
panel nyata selalu lewat `dry_run` → izin pengguna → satu panel uji → verifikasi.

## Yang sengaja TIDAK ada di repo ini

| Berkas | Alasan |
|---|---|
| `live_check.db` + dua salinannya | memuat 531 personel (nama, nomor kartu, NIK) + log akses |
| `Personnel_*.xls`, `Departmenet.xls` | ekspor data HR |
| `test_zkteco.db` | salinan DB uji |
| `.env` | sandi PostgreSQL + token push agent |
| `logs/`, `*.log` | jejak runtime, bisa memuat token/IP panel |
| `agent/installer/.cache/`, `dist/` | artefak build installer (puluhan MB) |

Semuanya diabaikan `.gitignore` sejak commit pertama, jadi tidak pernah masuk riwayat git.
Menambahkan paksa dengan `git add -f` akan membocorkannya **permanen** — riwayat git tetap
menyimpan blob-nya walau dihapus di commit berikutnya.
