---
description: "Use when running or starting the backend, using operational scripts (live_check, boot_check, check_panels, scan_range, import_devices_csv, verify_person_on_panels, make_bundle), or writing PowerShell/batch launchers on this Windows host."
applyTo: ["**/scripts/**", "**/deploy/**", "**/*.bat", "**/*.ps1", "**/.vscode/**"]
---
# Perintah & host dev (Windows)

## Jalankan backend
```powershell
# Cara cepat (tombol, disarankan): proses LATAR + supervisor auto-restart + reload .py/.html
.\jalankan-backend.bat      # Ctrl+Shift+B
.\status-backend.bat        # status + ekor log
.\hentikan-backend.bat      # hentikan
.\zkteco-backend\deploy\serve_backend.ps1 -Action stop -Force   # + bersihkan proses yatim
```
```powershell
# Manual. Env harus ada di proses SERVER — kalau PUSH_AGENT_URL kosong, push = 503.
$env:DATABASE_URL="sqlite:///C:/laragon/www/zkteco/zkteco-backend/live_check.db"
$env:PUSH_AGENT_URL="http://127.0.0.1:8081"
$env:PUSH_AGENT_TOKEN="<token>"
$env:SCHEDULER_ENABLED="false"
& "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir "C:\laragon\www\zkteco\zkteco-backend" --host 0.0.0.0 --port 8000
```
Launcher mencari `PUSH_AGENT_URL`/token dengan urutan **parameter → registry (User lalu Machine) → environment proses → default**. Registry menang karena shell task VS Code memakai environment yang di-snapshot saat VS Code dinyalakan (nilai `setx` terbaru tidak terlihat). Untuk token, `ZK_PUSH_AGENT_TOKEN` menang atas `PUSH_AGENT_TOKEN`. Di `logs/supervisor.log` selalu ada `push agent: <url> (<asal>)`, `token agen: <asal>`, dan hasil pemeriksaan saat start: `agen: OK` / `DITOLAK (401)` / tidak terhubung — **kalau semua push menjawab `Authorization: Bearer <token> wajib`, baca baris `agen:` ini lebih dulu**.
Port **8000** dipakai tim — jangan menyalakan instance kedua di port itu. Log: `zkteco-backend\logs\backend.log` + `supervisor.log`.

## Skrip operasional (jalankan dari `zkteco-backend/`; ganti `python` dengan `$PY` absolut kalau perlu)
| Kebutuhan | Perintah |
|---|---|
| Smoke test panel nyata | `python -m scripts.live_check --limit 5` |
| Server nyala end-to-end | `python -m scripts.boot_check` |
| Pre-flight routing ke panel | `python -m scripts.check_panels --csv ..\devices_input.csv` |
| Import device dari CSV | `python -m scripts.import_devices_csv ..\devices_input.csv` |
| Dump tabel panel (read-only) | `python -m scripts.inspect_panel_tables --ip <ip>` |
| Cari panel pada rentang IP (read-only) | `python -m scripts.scan_range 10.100.1.0/24 192.168.1.0/24` |
| Bandingkan satu orang di semua panel | `python -m scripts.verify_person_on_panels --badge <pin>` |
| **Satu-satunya skrip yang menulis panel** | `python -m scripts.check_push_write <ip>` — **minta izin user dulu** |
| Bundle deploy | `powershell -ExecutionPolicy Bypass -File .\deploy\make_bundle.ps1` |

## Jebakan host
- `python3` **tidak ada**. Interpreter host = 3.13 64-bit (`C:\Python313\python.exe`). Python **32-bit** khusus agent: `%LOCALAPPDATA%\Programs\Python\Python313-32\python.exe` (panggil dengan path lengkap; `py -0p` tidak menampilkannya).
- Nama import library = `c3` (`from c3 import C3`); `zkaccess_c3` **gagal**.
- Tool terminal sering membuang `Set-Location` → **selalu path absolut**; `.\.venv\Scripts\python.exe` bisa resolve ke venv yang **salah** (`.venv` root tidak punya `uvicorn`/`pytest`).
- Setelah SSH ke server, perintah berikutnya berjalan **di host remote** — `exit` dulu.
- PowerShell memakai `;` bukan `&&`. `Invoke-WebRequest` butuh `-UseBasicParsing` (kalau tidak, prompt interaktif "Script Execution Risk").
- Browser VS Code: `http://localhost:PORT`, **bukan** `http://127.0.0.1:PORT` (ERR_CONNECTION_REFUSED).
- Bash sungguhan: `C:\Program Files\Git\bin\bash.exe` (untuk `bash -n`); `bash` di PATH adalah stub WSL.
- Info curl: `get_terminal_output` pada terminal async yang baru bisa kosong walau server sudah listen — pastikan dengan permintaan HTTP nyata.

## Menulis launcher `.ps1` / `.bat` (tiga jebakan yang sudah memakan waktu)
- `Start-Process -ArgumentList` berupa **array** menambahkan spasi pada elemen ber-quote, sehingga `-Python "C:\…"` sampai sebagai `" C:\… "` dan diabaikan → pakai **satu string command-line utuh**.
- Menunggu pembungkus `cmd.exe` bisa menggantung selamanya setelah anaknya dibunuh → supervisor tidak pernah restart. Tunggu proses python-nya langsung (`WaitForExit()`).
- `ErrorActionPreference=Stop` + stderr proses native (mis. `taskkill` gagal) = *terminating error* yang **mematikan skrip di tengah** → bungkus `*> $null` + `EA='Continue'`.
- `.bat`: tanda kurung di dalam teks `echo` yang berada di blok `if (...)` merusak parsing cmd (`... was unexpected at this time`) → pakai `goto :label`. Tulis `.bat`/`.ps1` **ASCII tanpa BOM** (cmd & PowerShell 5.1 membacanya sebagai ANSI).
- Semua ini dikunci oleh `tests/test_dev_launcher.py`.
