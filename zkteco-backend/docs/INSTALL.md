# Panduan Instalasi — ZKTeco C3 Access Control

Dokumen ini menjelaskan cara memasang sistem dari nol, langkah demi langkah, untuk
semua skenario yang nyata dipakai di proyek ini.

> **Baca [`../../AGENTS.md`](../../AGENTS.md) dulu.** Dokumen itu adalah kontrak
> (aturan emas, fakta hardware yang sudah diverifikasi, jebakan lingkungan). Kalau
> ada yang bertentangan, `AGENTS.md` yang benar.
>
> Untuk cara **memakai** aplikasinya setelah terpasang, lihat
> [`TUTORIAL.md`](TUTORIAL.md). Untuk runbook deploy ke server, lihat
> [`DEPLOYMENT.md`](DEPLOYMENT.md).

Terakhir diverifikasi: **2026-09-17**.

---

## 1. Peta instalasi (jangan tercampur)

Sistem ini terdiri dari **tiga bagian terpisah**. Kesalahan paling umum adalah
menganggap semuanya satu aplikasi.

```mermaid
flowchart LR
    A["Backend FastAPI<br/>(Linux atau Windows)<br/>64-bit"] -- "baca: TCP 4370<br/>zkaccess-c3 in-process" --> P["23 panel<br/>ZKTeco C3<br/>10.100.1.x"]
    A -- "HTTP + token<br/>PUSH_AGENT_URL" --> G["Windows push agent<br/>32-bit Python<br/>plcommpro.dll"]
    G -- "tulis: TCP 4370<br/>Pull SDK resmi" --> P
    A -- "hanya baca (SELECT/WITH)" --> Z["ZKAccess SQL Server<br/>10.100.1.100\\SQLEXPRESS"]
    U["Operator<br/>browser"] --> A
```

| Bagian | Wajib? | Kenapa |
|---|---|---|
| **1. Backend** (FastAPI + DB) | Ya | Dashboard, API, penyimpanan data personel/level |
| **2. Windows push agent** | Hanya kalau perlu **menulis** ke panel | `plcommpro.dll` 32-bit tidak bisa dimuat backend 64-bit |
| **3. ZKAccess SQL Server** | Opsional (read-only) | Sumber data lama untuk migrasi / pembanding saat ragu |

**Yang harus dipahami sejak awal:** membaca panel dan menulis panel memakai jalur
berbeda. Backend membaca sendiri pakai library Python `zkaccess-c3`; menulis selalu
lewat agent Windows. Kalau `PUSH_AGENT_URL` kosong, semua push menjawab **503**
(tapi `dry_run` tetap jalan).

---

## 2. Prasyarat

| Kebutuhan | Untuk apa | Catatan |
|---|---|---|
| Python **3.10+** | Backend | Di Windows dev host: `C:\Python313\python.exe` (64-bit). `python3` **tidak ada** di Windows. |
| Python **32-bit** 3.8+ | Agent | Wajib. Python 64-bit tidak bisa memuat DLL 32-bit. |
| ZKTeco Pull SDK | Agent | `plcommpro.dll` **beserta semua `pl*.dll` lain** di folder yang sama. |
| VC++ Redistributable **x86** | Agent | Tanpa ini DLL gagal dimuat dengan pesan menyesatkan. |
| PostgreSQL 12+ | Produksi | SQLite cukup untuk dev lokal & tes. |
| Rute jaringan ke `10.100.1.0/24` TCP 4370 | Panel | **Gerbang utama.** Tanpa rute ini, produk tidak ada gunanya. |

### Instal Python 32-bit tanpa mengganggu yang 64-bit

Kalau 64-bit Python sudah ada (hampir pasti — backend membutuhkannya), pasang
32-bit ke folder terpisah dan jangan masukkan ke `PATH`:

```powershell
python-3.13.15.exe /passive InstallAllUsers=0 PrependPath=0 `
    Include_launcher=0 TargetDir="$env:LOCALAPPDATA\Programs\Python\Python313-32"
```

Ini instalasi per-user (tanpa admin) dan selalu dipanggil dengan **path lengkap**.
Interpreter 32-bit **tidak muncul** di `py -0p`.

---

## 3. Skenario A — Dev lokal di Windows (SQLite, paling sering dipakai)

Cocok untuk mengembangkan fitur, menjalankan tes, dan menguji panel dari laptop.

### A.1 Virtualenv + dependensi

Venv yang benar ada di dalam `zkteco-backend/`. Venv di root workspace
(`c:\laragon\www\zkteco\.venv`) **tidak** punya `uvicorn`/`pytest` — jangan dipakai.

```powershell
& "C:\Python313\python.exe" -m venv "C:\laragon\www\zkteco\zkteco-backend\.venv"
& "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe" -m pip install -r "C:\laragon\www\zkteco\zkteco-backend\requirements.txt"
```

### A.2 Skema database

Alembic adalah sumber kebenaran skema. Rantai migrasinya **linear**:

```
795ec8945bac  →  39d8af1b06df  →  907835756e6e  →  69f13e9af7f0  →  a1d4c7b90e35  (head)
```

```powershell
Push-Location "C:\laragon\www\zkteco\zkteco-backend"
& ".\.venv\Scripts\python.exe" -m alembic heads     # harus a1d4c7b90e35, SATU baris
& ".\.venv\Scripts\python.exe" -m alembic upgrade head
Pop-Location
```

> `Base.metadata.create_all` hanya untuk DB baru. Jangan pakai untuk mengubah DB
> yang sudah ada — itu membuat skema dan Alembic berbeda diam-diam.

### A.3 Jalankan backend

**Cara cepat (disarankan): tombol launcher.** Backend dinyalakan sebagai proses
**latar** — tetap hidup setelah jendela/VS Code ditutup, dan supervisor-nya
menyalakan ulang otomatis kalau prosesnya mati (crash), jadi server tidak lagi
"mati diam-diam".

| Tombol | Fungsi |
|---|---|
| `jalankan-backend.bat` | Nyalakan backend di latar (port 8000) + tunggu sampai `/health` menjawab |
| `status-backend.bat` | JALAN/MATI, PID supervisor, pemakai port, `/health`, ekor log |
| `hentikan-backend.bat` | Hentikan supervisor + semua anak prosesnya, pastikan port bebas |
| VS Code: **Ctrl+Shift+B** | Sama dengan `jalankan-backend.bat` (task default) |
| VS Code: Terminal ▸ Run Task | `Jalankan` / `Hentikan` / `Status` / `Ikuti log backend` |

Yang dipasang launcher (lihat `zkteco-backend/deploy/serve_backend.ps1`):

- `DATABASE_URL` → `sqlite:///<repo>/live_check.db` dan `SCHEDULER_ENABLED=false`
  (scheduler hanya di proses worker — panel hanya menerima **satu** koneksi)
- `PUSH_AGENT_URL` dan token dibaca dengan urutan **parameter (`-PushAgentUrl` /
  `-PushAgentToken`) → registry (User, lalu Machine) → environment proses →
  default** `http://127.0.0.1:8081`. Registry didahulukan karena shell task VS
  Code memakai environment yang di-snapshot saat VS Code menyala, sehingga nilai
  `setx` yang lebih baru tidak terlihat di sana (dulu itu membuat push menuju
  agen lokal tanpa pesan apa pun). Untuk token, **`ZK_PUSH_AGENT_TOKEN` menang
  atas `PUSH_AGENT_TOKEN`** kalau keduanya ada — nilai lama di
  `PUSH_AGENT_TOKEN` pernah membuat semua push menjawab 401.
- Asal alamat dan token dicatat di `logs/supervisor.log`
  (`push agent: <url> (<asal>)`, `token agen: <asal>`), dan launcher **memeriksa
  token ke `/health` agen saat start**: baris `agen: ...` berbunyi `OK`, `DITOLAK
  (401)`, atau tidak terhubung. Pemeriksaan itu tidak pernah menghentikan
  backend.
- `--reload --reload-include *.html` → perubahan `*.py` **dan**
  `app/static/dashboard.html` langsung dipakai tanpa restart manual
- Log di `zkteco-backend/logs/`: `backend.log` (uvicorn), `backend.log.1` (run
  sebelumnya — ini yang dibaca setelah crash), `supervisor.log` (kapan dan kenapa
  di-restart), `backend.out.log`, dan `backend.pid`

Argumen yang berguna: `-Port 8010`, `-NoReload`, `-DatabaseUrl "...`, dan
`-Action stop -Force` untuk membersihkan proses python yatim yang masih memegang
port (muncul kalau reloader uvicorn dibunuh manual). Agent push
(`zk_push_agent.py`) tidak pernah diganggu oleh pembersihan itu.

**Cara manual** (kalau ingin mengontrol sendiri; database dev yang nyata adalah
**`live_check.db`** — SQLite, 23 device / 531 personel — bukan Postgres):

```powershell
$env:DATABASE_URL="sqlite:///C:/laragon/www/zkteco/zkteco-backend/live_check.db"
$env:SCHEDULER_ENABLED="false"
$env:PUSH_AGENT_URL="http://127.0.0.1:8081"     # kosongkan kalau agent belum jalan
$env:PUSH_AGENT_TOKEN="<token-agent>"
& "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe" -m uvicorn app.main:app `
  --app-dir "C:\laragon\www\zkteco\zkteco-backend" --host 0.0.0.0 --port 8000
```

- Dashboard: <http://localhost:8000/> · Swagger: <http://localhost:8000/docs>
- **Login pertama:** `admin` / `admin` dan `hr` / `hr`. Akun ini dibuat otomatis
  saat start **hanya kalau tabel `users` masih kosong**, dari `AUTH_SEED_USERS`
  (`user:password:role`, dipisah koma) — ganti variabelnya sebelum start pertama
  kalau password itu tidak boleh dipakai. Dashboard terus memperingatkan sampai
  password-nya diganti (tombol **Ganti password**; tab **Pengguna** untuk akun lain).
  `admin` boleh membuka semua tab; `hr` hanya Personel, Access Level, Departemen,
  dan Monitoring.
- `SCHEDULER_ENABLED=false` itu **wajib** di proses API. Panel hanya menerima
  **satu** koneksi sekaligus; dua poller paralel membuat panel tampak offline.
- `PUSH_AGENT_URL` harus ada di environment **server**, bukan cuma di klien.
- Browser VS Code harus memakai `http://localhost:8000`, **bukan**
  `http://127.0.0.1:8000` (selalu `ERR_CONNECTION_REFUSED`).
- `app/main.py` membaca `dashboard.html` **saat import** → cara manual butuh
  restart server tiap kali UI diubah (launcher di atas sudah otomatis).

### A.4 Isi daftar panel

```powershell
Push-Location "C:\laragon\www\zkteco\zkteco-backend"
& ".\.venv\Scripts\python.exe" -m scripts.check_panels --csv ..\devices_input.csv
& ".\.venv\Scripts\python.exe" -m scripts.import_devices_csv ..\devices_input.csv
& ".\.venv\Scripts\python.exe" -m scripts.live_check --limit 5
Pop-Location
```

`check_panels` adalah **gerbang**: kalau `0/23 terjangkau`, jangan lanjut — host
tidak punya rute ke jaringan panel. `devices_input.csv` berisi 23 panel dan boleh
ditambah baris.

### A.5 Tes + lint (tanpa hardware, aman kapan saja)

```powershell
& "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe" -m pytest -q `
  --rootdir "C:\laragon\www\zkteco\zkteco-backend" "C:\laragon\www\zkteco\zkteco-backend\tests"
& "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe" -m ruff check "C:\laragon\www\zkteco\zkteco-backend"
& "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe" -m ruff format --check "C:\laragon\www\zkteco\zkteco-backend"
```

Hasil yang benar: **247 passed**, ruff bersih. Tes **tidak boleh** butuh panel —
device palsu dipasang lewat `app.services.device_client.set_client_factory()`.

> Pakai **path absolut** di semua perintah. Terminal tool sering membuang
> `Set-Location` di awal, sehingga `.\.venv\Scripts\python.exe` bisa resolve ke venv
> yang salah. `--rootdir` penting karena `pyproject.toml` menaruh `testpaths=["tests"]`.

---

## 4. Skenario B — Produksi Linux (bare-metal, systemd)

Asumsi target: Ubuntu, aplikasi di `/opt/zkteco-backend`, user sistem `zkteco`,
port **8000** (80/443 biasanya sudah dipakai situs lain).

### B.0 Pre-flight WAJIB sebelum memasang apa pun

```bash
# di host tujuan — tidak perlu instalasi apa pun
python3 -m scripts.check_panels --csv devices_input.csv
```

- `23/23 terjangkau` → lanjut.
- `0/23 terjangkau` → **stop**. Perbaiki routing/firewall, atau jalankan backend di
  host yang punya rute (mis. mesin Windows dev yang sudah terbukti bisa).

### B.1 Bangun bundle (di Windows)

```powershell
Push-Location "C:\laragon\www\zkteco\zkteco-backend"
powershell -ExecutionPolicy Bypass -File .\deploy\make_bundle.ps1
Pop-Location
```

Menghasilkan `deploy/zkteco-backend-<timestamp>.tar.gz` (~45 KB) berisi `app/`,
`migrations/`, `scripts/`, `tests/`, `deploy/`, `requirements.txt`, `.env.example`,
dan `devices_input.csv`. `.venv`, `*.db`, `.env`, cache, dan `.git` dikecualikan.

### B.2 Salin ke server

```powershell
scp "C:\laragon\www\zkteco\zkteco-backend\deploy\zkteco-backend-<timestamp>.tar.gz" sentral@192.168.0.27:/tmp/
```

```bash
# di server
sudo tar -xzf /tmp/zkteco-backend-<timestamp>.tar.gz -C /tmp
sudo rm -rf /opt/zkteco-backend
sudo mv /tmp/zkteco-backend /opt/zkteco-backend
cd /opt/zkteco-backend
```

### B.3 PostgreSQL

```bash
sudo apt update && sudo apt install -y postgresql
sudo -u postgres psql <<'SQL'
CREATE USER zkteco WITH PASSWORD 'CHANGE_ME';
CREATE DATABASE zkteco OWNER zkteco;
SQL
```

### B.4 Konfigurasi `.env`

```bash
sudo cp .env.example .env
sudo nano .env
```

Minimal yang harus benar:

```ini
DATABASE_URL=postgresql+psycopg://zkteco:CHANGE_ME@localhost:5432/zkteco
SCHEDULER_ENABLED=false        # unit worker yang menyalakannya
LOG_LEVEL=INFO
PUSH_AGENT_URL=http://<windows-agent>:8081    # opsional; kosong = push 503
PUSH_AGENT_TOKEN=<token-agent>
```

Variabel lain (semua punya default): `DEVICE_PORT=4370`, `DEVICE_MAX_RETRIES=3`,
`DEVICE_RETRY_BACKOFF=0.5`, `DEVICE_PARALLELISM=8`, `LOG_POLL_INTERVAL_SECONDS=60`,
`HEALTH_CHECK_INTERVAL_SECONDS=120`, `PUSH_AGENT_TIMEOUT=60`.

### B.5 Jalankan installer

```bash
sudo bash deploy/install.sh
```

Pakai `bash`, **bukan** `./deploy/install.sh` — tar yang dibuat di Windows
kehilangan executable bit.

Yang dilakukan `install.sh` (aman diulang untuk upgrade):

1. cek prasyarat (`python3`, `venv`, Python ≥ 3.10)
2. buat user sistem `zkteco` (tanpa login shell)
3. buat `.venv` + `pip install -r requirements.txt`
4. buat `.env` dari contoh kalau belum ada (**tidak** menimpa yang sudah ada)
5. `alembic upgrade head`
6. pasang `zkteco-api.service` + `zkteco-worker.service`, `enable --now`
7. probe `http://127.0.0.1:8000/health`

> API dijalankan dengan `SCHEDULER_ENABLED=false`, worker dengan `true`. Kalau
> keduanya `true`, setiap job poll berjalan dobel dan log masuk dua kali.

### B.6 Muat panel dan verifikasi

```bash
cd /opt/zkteco-backend
.venv/bin/python -m scripts.check_panels --csv devices_input.csv   # gerbang
.venv/bin/python -m scripts.import_devices_csv devices_input.csv
.venv/bin/python -m scripts.live_check --csv devices_input.csv --limit 25
curl -s http://127.0.0.1:8000/api/dashboard/devices | head -c 400
```

Dashboard: `http://<server>:8000/`

### B.7 Reverse proxy (opsional)

`deploy/nginx-zkteco.conf` sudah menyertakan `server_name` terpisah dan timeout
300 s untuk endpoint sapuan armada. Salin ke `sites-available`, ganti
`server_name zkteco.local`, lalu `nginx -t && systemctl reload nginx`.

### B.8 Operasi harian

```bash
systemctl status zkteco-api zkteco-worker
journalctl -u zkteco-api -f
journalctl -u zkteco-worker -f
curl -s localhost:8000/health

curl -X POST localhost:8000/api/devices/sync/refresh   # refresh armada sekarang
curl -X POST localhost:8000/api/logs/pull              # tarik log sekarang
```

Hasil tiap job per device tersimpan di tabel `sync_runs` — tempat pertama yang
dilihat kalau ada panel yang diam.

---

## 5. Skenario C — Docker Compose

Hanya kalau Docker tersedia di host (server produksi saat ini **belum** ada Docker).

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER      # logout/login lagi
cd /opt/zkteco-backend
sudo cp .env.example .env          # set POSTGRES_PASSWORD
sudo docker compose up -d --build
sudo docker compose logs -f backend
```

Tiga service: `db` (PostgreSQL 16), `backend` (API, `SCHEDULER_ENABLED=false`),
`worker` (`SCHEDULER_ENABLED=true`). Scheduler sengaja dipisah supaya worker
uvicorn tambahan tidak memulai salinan job yang sama.

---

## 6. Skenario D — Windows push agent (jalur tulis)

Ikuti [`../agent/README.md`](../agent/README.md) untuk detail lengkap; ini ringkasannya.

### D.0 Cara cepat: installer (disarankan)

Jalankan **sebagai Administrator**:

```powershell
ZkPushAgent-Setup.exe
```

Wizard-nya meminta folder SDK (mis. `C:\PullSDK`, harus berisi semua `pl*.dll`),
token, port, dan bind host. Untuk banyak PC sekaligus:

```powershell
\\share\ZkPushAgent-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
    /TOKEN=<token> /SDK="C:\PullSDK" /HOST=0.0.0.0 /PORT=8081
```

Installer membawa Python 3.13 embeddable **32-bit** sendiri (Python mesin tidak
 disentuh), memasang VC++ x86 kalau perlu, menyalin `pl*.dll` ke satu folder di
folder aplikasi (pencegah `PullLastError=-201`), menulis environment variable,
mendaftarkan task `ZkPushAgent`, lalu menyalakan agen dan membuktikannya dengan
`GET /health` - hasilnya di `CHECK-REPORT.txt` dan tokennya di
`BACKEND-SNIPPET.txt`. Langkah D.1-D.4 di bawah adalah versi manualnya; lihat
[`../agent/installer/README.md`](../agent/installer/README.md) untuk operasi
(token, restart, uninstall, troubleshooting) dan cara membangun installer.

### D.1 Siapkan DLL

Letakkan **semua** `pl*.dll` (plcommpro, plcomms, plrscagent, plrscomm, pltcpcomm,
plusbcomm) di satu folder, mis. `C:\PullSDK\`. Ambil dari SDK resmi ZKTeco
(Download Center → *SDK*; situsnya berbasis JS sehingga tidak bisa di-script).
`plcommpro.dll` yang terverifikasi melaporkan versi **2.2.0.220**.

### D.2 Set environment variable

```powershell
setx ZK_PULLSDK_DLL "C:\PullSDK\plcommpro.dll"
setx ZK_PUSH_AGENT_TOKEN "<token-acak-minimal-32-karakter>"
setx ZK_PUSH_AGENT_PORT "8081"
```

| Variabel | Default | Arti |
|---|---|---|
| `ZK_PUSH_AGENT_TOKEN` | — | **Wajib.** Agent menolak start tanpa ini. |
| `ZK_PULLSDK_DLL` | `plcommpro.dll` | Path lengkap DLL. |
| `ZK_PUSH_AGENT_HOST` | `127.0.0.1` | Pakai `0.0.0.0` hanya kalau backend di mesin lain. |
| `ZK_PUSH_AGENT_PORT` | `8081` | Port listen. |
| `ZK_PANEL_PASSWORD` | kosong | Password default panel yang memerlukannya. |
| `ZK_AGENT_BUFFER_SIZE` | `65536` | Naikkan untuk panel dengan user sangat banyak. |

### D.3 Uji sebelum jalan

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313-32\python.exe" check_setup.py --test 10.100.1.12
```

Output sehat berisi `OK Python ... (32-bit)`, `OK export SetDeviceData: True`, dan
`OK konek ke 10.100.1.12: ~SerialNumber=...`. Langkah `--test` inilah satu-satunya
cara menangkap jebakan `-201` sebelum push.

### D.4 Jalankan agent

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313-32\python.exe" zk_push_agent.py
```

Agent **gagal cepat** daripada setengah jalan: exit `2` = token belum diset,
exit `3` = DLL gagal dimuat.

```powershell
curl http://127.0.0.1:8081/health -H "Authorization: Bearer <token>"
# {"ok": true, "dll": "C:\\PullSDK\\plcommpro.dll", "python_bits": 32, "pull_last_error": 0}
```

Untuk service: Task Scheduler (*Run whether user is logged on or not*, trigger *At
startup*, tick *Restart the task if it fails*), atau
`nssm install ZkPushAgent "<path python32>" zk_push_agent.py` (isi *Start in* = folder agent).

### D.5 Hubungkan backend ke agent

Set di **environment server backend**, lalu restart:

```ini
PUSH_AGENT_URL=http://<windows-agent>:8081
PUSH_AGENT_TOKEN=<token yang sama>
```

### D.6 Buktikan jalur tulis pada SATU panel

**Jangan pernah** menulis ke panel nyata tanpa persetujuan user. Urutan wajib:
`dry_run` → laporkan → izin → satu panel uji → verifikasi → baru meluas.

```bash
# 1. lihat saja, tidak menulis
curl -X POST "http://localhost:8000/api/devices/1/personnel/sync?dry_run=true"

# 2. (setelah izin) satu panel uji, aman & mengembalikan panel ke kondisi semula
python -m scripts.check_push_write 10.100.1.12
```

`check_push_write.py` adalah **satu-satunya** skrip di `scripts/` yang menulis ke
device. Sifat keamanannya: hanya menulis baris `user` (tanpa `userauthorize`,
sehingga user uji tidak bisa membuka pintu apa pun), tanpa nomor kartu, menolak
panel dengan >200 user, menolak kalau PIN uji sudah ada, dan membandingkan daftar
PIN penuh sebelum/sesudah.

---

## 7. Skenario E — Migrasi dari ZKAccess 3.5

Sumbernya SQL Server ZKAccess, dibaca **read-only** (`ZKAccessSource` menolak
statement selain `SELECT`/`WITH`). Kredensial selalu dari environment, tidak pernah
dari kode.

```powershell
& ".\.venv\Scripts\python.exe" -m pip install pyodbc    # butuh ODBC Driver for SQL Server
$env:ZKACCESS_SERVER='10.100.1.100\SQLEXPRESS'
$env:ZKACCESS_USER='sa'
$env:ZKACCESS_PASSWORD='<password>'

& ".\.venv\Scripts\python.exe" -m scripts.check_zkaccess_source    # konektivitas + guard read-only
& ".\.venv\Scripts\python.exe" -m scripts.migrate_from_zkaccess --dry-run
& ".\.venv\Scripts\python.exe" -m scripts.migrate_from_zkaccess    # idempoten
& ".\.venv\Scripts\python.exe" -m scripts.verify_migration         # rekonsiliasi per kategori
```

Yang dimigrasikan (log akses **sengaja dilewati**):

| ZKAccess | baris | menjadi |
|---|---|---|
| `Machines` | 23 | `Device` (dicocokkan pada IP+port) |
| `USERINFO` | 528 | `Personnel` (`Badgenumber` → `employee_id` + `pin`) |
| `DEPARTMENTS` | 28 | `departments` (master hirarki) |
| `acc_levelset` | 12 | `AccessGroup` |
| `acc_levelset_door_group` | 87 | `access_group_doors` |
| `acc_levelset_emp` | 2 037 | `personnel_access_groups` |
| `USERINFO.CardNo` | 524 | `Personnel.card_number` |

Hasil `verify_migration` yang benar:

```
Devices            23 = 23
Personel          528 = 528          kartu tidak cocok: 0
Grup akses         12 = 12
Grup -> pintu      87 = 87
Keanggotaan      2037 = 2037
```

**ZKAccess tetap online sebagai otoritas fallback.** Kalau data di sini
mencurigakan, baca dari ZKAccess read-only untuk membandingkan.

---

## 8. Checklist verifikasi akhir

| # | Cek | Perintah / cara | Hasil benar |
|---|---|---|---|
| 1 | Tes tanpa hardware | `python -m pytest` | 247 passed |
| 2 | Lint | `python -m ruff check .` + `ruff format --check .` | bersih |
| 3 | Satu head migrasi | `python -m alembic heads` | `a1d4c7b90e35` (satu baris) |
| 4 | Rute ke panel | `python -m scripts.check_panels --csv devices_input.csv` | > 0/23 (target 23/23) |
| 5 | Panel terdaftar | `python -m scripts.import_devices_csv devices_input.csv` | 23 device |
| 6 | Panel sungguhan terbaca | `python -m scripts.live_check --limit 5` | serial + firmware terbaca |
| 7 | Server hidup | `python -m scripts.boot_check` | endpoint menjawab |
| 8 | Dashboard | buka `http://localhost:8000/` | 23 device, jumlah personel muncul |
| 9 | Agen siap | `curl .../health` di port 8081 | `python_bits: 32`, `ok: true` |
| 10 | Push siap | `?dry_run=true` | daftar record, tanpa error 503 |

---

## 9. Troubleshooting

| Gejala | Penyebab | Tindakan |
|---|---|---|
| Semua device `offline`, `/health` OK | Tidak ada rute ke `10.100.1.x` | `python -m scripts.check_panels` |
| Terlihat 20/23 online, lalu semua online saat dicek satu-satu | Panel hanya menerima **satu** koneksi | Jangan perlakukan gagal koneksi tunggal sebagai panel mati; ulangi sekuensial, pastikan `SCHEDULER_ENABLED=false` di API |
| Push menjawab **503** | `PUSH_AGENT_URL` kosong **di environment server** | Set di server, restart, cek `/health` agent |
| Push menjawab **502** | Agent/panel gagal | Lihat log agent, ulangi (kode `-2`/`-107` transien) |
| Exit `3`, DLL gagal dimuat | Python 64-bit, atau VC++ x86 belum ada | Pakai path 32-bit penuh; pasang `vc_redist.x86.exe` |
| `Connect()` gagal `PullLastError=-201` | SDK menyelesaikan DLL transport relatif ke **working directory** | Agent sudah `os.chdir()` ke folder SDK; kalau memanggil DLL sendiri, samakan working directory atau copy `pl*.dll` ke `C:\Windows\SysWOW64` |
| Log dobel | Dua scheduler jalan | `SCHEDULER_ENABLED=false` di unit API |
| Log error `-112` | Balasan lebih besar dari buffer 64 KB (mis. `transaction` pada panel dengan log besar) | Naikkan `ZK_AGENT_BUFFER_SIZE` (mis. `4194304`) lalu restart agent. Tarik log memang lewat agent (`LogService._read_transactions`); jalur library tidak punya buffer yang bisa dinaikkan dan hanya dipakai sebagai cadangan |
| Dashboard kosong setelah import | DB berbeda (env var salah) | `curl localhost:8000/api/dashboard/summary` |
| Perubahan `dashboard.html` tidak muncul | HTML dibaca saat import | Restart backend |
| `invalid literal for int()` | Bug parsing library/firmware | Sudah ditambal di `app/services/c3_compat.py` |
| `vcruntime140.dll` / `module not found` saat `import c3` | Nama import salah | Namanya **`c3`**: `from c3 import C3`. `zkaccess_c3` gagal. |

---

## 10. Upgrade, rollback, dan hal yang tidak boleh dilakukan

### Upgrade

```bash
# rebundle di Windows → scp → lalu di server:
sudo tar -xzf /tmp/zkteco-backend-<baru>.tar.gz -C /tmp
sudo systemctl stop zkteco-worker zkteco-api
sudo rsync -a --delete /tmp/zkteco-backend/app/        /opt/zkteco-backend/app/
sudo rsync -a --delete /tmp/zkteco-backend/migrations/ /opt/zkteco-backend/migrations/
sudo rsync -a --delete /tmp/zkteco-backend/scripts/    /opt/zkteco-backend/scripts/
cd /opt/zkteco-backend
sudo -u zkteco .venv/bin/pip install -q -r requirements.txt
sudo -u zkteco bash -c 'set -a; . ./.env; set +a; .venv/bin/python -m alembic upgrade head'
sudo systemctl start zkteco-api zkteco-worker
```

### Rollback

```bash
sudo systemctl stop zkteco-api zkteco-worker
sudo -u zkteco bash -c 'set -a; . ./.env; set +a; .venv/bin/python -m alembic downgrade -1'
# kembalikan bundle kode sebelumnya, lalu start lagi
```

### Pantangan

1. **Jangan** memuat `plcommpro.dll` di backend (DLL 32-bit, interpreter 64-bit).
2. **Jangan** mengubah skema lewat `create_all` pada DB yang sudah ada.
3. **Jangan** mengedit revisi Alembic yang sudah ada — buat revisi baru.
4. **Jangan** menyalakan scheduler di proses API.
5. **Jangan** menulis ke panel nyata tanpa `dry_run` + izin user + uji satu panel.
6. **Jangan** menghapus `live_check.db` (database dev nyata) atau `devices_input.csv`.
