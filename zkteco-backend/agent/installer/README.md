# Installer agen push (Windows) — `ZkPushAgent-Setup.exe`

Agen push (`../zk_push_agent.py`) adalah satu-satunya jalur **tulis** ke panel. Ia
butuh tiga hal yang mudah salah dipasang manual: Python **32-bit**, DLL resmi
ZKTeco (`pl*.dll`) di satu folder, dan VC++ Redistributable x86. Installer ini
mengerjakan ketiganya sekaligus, lalu **membuktikan** hasilnya lewat `GET /health`.

```
ZkPushAgent-Setup.exe  ->  {app}\python\python.exe   (3.13 embeddable 32-bit, dibundel)
                           {app}\sdk\*.dll           (disalin dari folder SDK Anda)
                           env mesin ZK_*            (token, port, host, path DLL)
                           Task "ZkPushAgent"        (SYSTEM, mulai saat boot, restart 1 menit)
                           {app}\CHECK-REPORT.txt    (bukti pemeriksaan)
```

## 1. Cara pakai cepat

1. Pastikan SDK resmi ZKTeco ada di satu folder (mis. `C:\PullSDK`) berisi
   **semua** `pl*.dll`: `plcommpro.dll`, `plcomms.dll`, `plrscagent.dll`,
   `plrscomm.dll`, `pltcpcomm.dll`, `plusbcomm.dll`. Kalau hanya `plcommpro.dll`
   yang ada, `Connect()` akan gagal dengan `PullLastError=-201` (installer
   memperingatkan ini saat Anda memilih folder).
2. Jalankan `ZkPushAgent-Setup.exe` **sebagai Administrator**.
3. Wizard: pilih folder SDK → isi token/port/host (boleh dibiarkan kosong,
   installer membuat token acak 30 byte) → opsional isi **satu** IP panel untuk
   uji baca-saja.
4. Halaman terakhir menampilkan `VERDICT: OK` dan token. Tempel baris dari
   `{app}\BACKEND-SNIPPET.txt` ke environment **server backend**, lalu restart
   backend:

```ini
PUSH_AGENT_URL=http://<nama-pc>:8081
PUSH_AGENT_TOKEN=<token dari installer>
```

Kalau backend ada di mesin lain, pilih bind host `0.0.0.0`; installer membuka
aturan firewall untuk port itu (`remoteip=localsubnet`, hanya jaringan lokal).

## 2. Pemasangan senyap (banyak PC)

```powershell
\\share\ZkPushAgent-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
    /TOKEN=<token-yang-sama-di-semua-pc> /SDK="C:\PullSDK" `
    /HOST=0.0.0.0 /PORT=8081
```

| Parameter | Arti | Default |
|---|---|---|
| `/TOKEN=<rahasia>` | Token bersama. **Kosongkan = dibuat otomatis** (di halaman terakhir / `CHECK-REPORT.txt`). | dibuat otomatis |
| `/SDK=<folder>` | Folder berisi `pl*.dll`. Wajib ada; tanpa `plcommpro.dll` pemasangan digagalkan. | hasil auto-deteksi (`C:\PullSDK`, `C:\ZKAccess`, `%ProgramFiles(x86)%\ZKTeco`, ...) |
| `/HOST=<ip>` | `127.0.0.1` = hanya mesin ini, `0.0.0.0` = bisa dihubungi mesin lain (buka firewall). | `127.0.0.1` |
| `/PORT=<port>` | Port listen agen (1024-65535). | `8081` |
| `/PANELIP=<ip>` | IP satu panel untuk uji koneksi **baca-saja** saat pemasangan. | tidak diuji |
| `/PANELPWD=<rahasia>` | Password panel (kalau panel memerlukannya). | kosong |
| `/BUFFER=<byte>` | `ZK_AGENT_BUFFER_SIZE`; naikkan untuk panel berlog besar. | `65536` |
| `/DIR=<folder>` | Folder pemasangan. | `C:\Program Files (x86)\ZkPushAgent` |

Catatan: kalau parameter tidak diberikan dan mesin ini **sudah** punya agen
terpasang, installer memakai kembali nilai lama (terutama token) — upgrade tidak
diam-diam mengganti token yang sudah dipakai backend.

Penting soal **level** environment: installer membaca/menulis level **Machine**,
sedangkan `setx` (perintah yang dipakai di `../README.md`) menulis level **User**.
Kalau PC ini punya `ZK_*` hanya di level User, wizard akan menampilkan kolom token
KOSONG dan membuat token **baru** — backend harus diganti `PUSH_AGENT_TOKEN`-nya.
Kalau tidak ingin menyentuh backend, ambil token lama lalu kirim eksplisit:

```powershell
$lama = [Environment]::GetEnvironmentVariable('ZK_PUSH_AGENT_TOKEN', 'User')
& .\ZkPushAgent-Setup.exe /TOKEN=$lama /SDK="C:\PullSDK"
```

## 3. Isi folder setelah terpasang

| Berkas | Guna |
|---|---|
| `python\` | Python 3.13 embeddable 32-bit. Terpisah dari Python 64-bit mesin. |
| `sdk\` | Salinan semua `pl*.dll` dari folder SDK Anda. Working directory agen = folder ini, sehingga DLL transport selalu ketemu (pencegah `-201`). |
| `zk_push_agent.py`, `check_setup.py` | Agen dan pemeriksa prasyaratnya. |
| `run_agent.cmd` | Launcher yang dipakai task: menjalankan agen, menulis `agent.log`, memutar log di 5 MB. |
| `install_task.ps1` | Daftar/hapus task `ZkPushAgent`. Menolak Python 64-bit. |
| `check_agent.ps1` | Pemeriksaan lengkap: Python 32-bit, DLL, `check_setup.py`, token, task, `GET /health`. |
| `gen_token.ps1` | Buat token acak (CSPRNG) untuk memutar token. |
| `CHECK-REPORT.txt` | Hasil pemeriksaan saat pemasangan. |
| `BACKEND-SNIPPET.txt` | Baris `PUSH_AGENT_URL` / `PUSH_AGENT_TOKEN` untuk backend. |
| `agent.log` | Ada di `C:\ProgramData\ZkPushAgent\agent.log` (bukan di folder aplikasi). |

## 4. Operasi harian

```powershell
# Periksa semuanya (aman dijalankan saat agen bekerja)
powershell -ExecutionPolicy Bypass -File "C:\Program Files (x86)\ZkPushAgent\check_agent.ps1"

# Uji ke satu panel (baca-saja: nomor seri + firmware)
powershell -ExecutionPolicy Bypass -File "...\check_agent.ps1" -PanelIp 10.100.1.12

# Restart agen (mis. setelah mengganti token)
powershell -ExecutionPolicy Bypass -File "...\check_agent.ps1" -Restart

# Putar token: agen DAN backend harus diganti bersamaan
powershell -ExecutionPolicy Bypass -File "...\gen_token.ps1"
Stop-ScheduledTask -TaskName ZkPushAgent ; Start-ScheduledTask -TaskName ZkPushAgent

# Status task / log
Get-ScheduledTask ZkPushAgent | Select-Object TaskName, State
Get-Content "$env:ProgramData\ZkPushAgent\agent.log" -Tail 40 -Wait
```

Uninstall lewat **Add or remove programs** (atau `unins000.exe` di folder
aplikasi). Uninstaller menghentikan & menghapus task, menghapus aturan firewall,
dan menghapus keenam environment variable `ZK_*`. Log di
`C:\ProgramData\ZkPushAgent\` sengaja dibiarkan (untuk diagnosis).

## 5. Membangun installer

```powershell
# Sekali saja: Inno Setup 6 (winget, ~10 MB) + payload Python (9,3 MB) + VC++ x86 (13 MB)
powershell -ExecutionPolicy Bypass -File build_installer.ps1 -InstallInnoSetup

# Ulang setelah mengubah agen atau payload:
powershell -ExecutionPolicy Bypass -File build_installer.ps1
# -> dist\ZkPushAgent-Setup.exe  (+ dist\ZkPushAgent-Setup-<versi>.exe, dist\BUILD-INFO.txt)
```

Parameter: `-Version` (default `1.0.<yyMMdd>`), `-PythonVersion` (default 3.13.x
terbaru dari python.org), `-InnoSetupPath`, `-InstallInnoSetup`, `-Refresh`.

### 5b. Uji installer tanpa Administrator (`smoke_test.ps1`)

Pendaftaran task dan penulisan env mesin butuh Administrator, jadi installer utuh
tidak bisa diuji di terminal biasa. `smoke_test.ps1` menutup celah itu: ia
mengompilasi **salinan** `.iss` yang ditambal (hanya tiga hal - `PrivilegesRequired`
jadi `lowest`, `NeedVcRedist` dimatikan supaya `vc_redist` tidak minta elevasi,
nama keluaran jadi `ZkPushAgent-Smoke`), memasangnya senyap ke folder sementara,
memeriksa hasilnya, lalu meng-uninstall.

```powershell
powershell -ExecutionPolicy Bypass -File smoke_test.ps1          # port uji 8099
powershell -ExecutionPolicy Bypass -File smoke_test.ps1 -Keep     # simpan folder kerja untuk diagnosis
```

Yang diperiksa: berkas terpasang lengkap, `CHECK-REPORT.txt` memuat hasil
`GET /health` (`"python_bits": 32`), `BACKEND-SNIPPET.txt` berisi URL+token yang
benar, tidak ada proses tertinggal di port uji, dan uninstaller membersihkan
folder (termasuk `sdk\`).

**Uji ini menemukan bug nyata yang tidak terlihat dari kompilasi:** `FindSdkFolder()`
memanggil `ExpandConstant('{app}')` selama `InitializeWizard` (konstanta itu belum
punya nilai di sana) sehingga installer mati dengan *attempt to expand the "app"
constant before it was initialized* sebelum halaman pertama - persis error yang
paling mahal: baru ketahuan di PC orang lain.

Parameter: `-Port` (default `8099`, harus bebas), `-Sdk` (default `C:\PullSDK`),
`-InnoSetupPath`, `-Keep`.

Skrip build **memverifikasi payload sebelum mengemas**, dan berhenti kalau salah:

- `python.exe` payload harus **32-bit** (64-bit = DLL tidak akan termuat);
- `check_setup.py` harus jalan dengan interpreter itu, memakai tata letak seperti
  hasil pemasangan. Ini bukan formalitas: interpreter *embeddable* tidak
  menambahkan folder skrip ke `sys.path` (berkas `._pth` menggantikannya), jadi
  `build_installer.ps1` menambahkan baris `..` ke `python313._pth` supaya
  `check_setup.py` bisa mengimpor `zk_push_agent`;
- semua skrip payload harus **ASCII tanpa BOM** dan lolos parser PowerShell;
- `.iss` baru dikompilasi setelah itu.

Artefak `.cache\` dan `dist\` bukan sumber dan tidak ikut masuk bundle deploy.

## 6. Yang **tidak** dilakukan installer ini

- **Tidak menulis apa pun ke panel.** Satu-satunya sentuhan ke panel adalah
  `check_setup.py --test <ip>`, dan itu baca-saja (`GetDeviceParam`), hanya kalau
  Anda mengisi `/PANELIP`.
- **Tidak mengemas SDK ZKTeco.** `pl*.dll` disalin dari folder yang Anda tunjuk
  (lisensi SDK tidak memungkinkan redistribusi). Yang ikut dikemas hanya Python
  resmi dan `vc_redist.x86.exe` dari Microsoft.
- **Tidak memasang backend.** Backend tetap di server Linux (`deploy/install.sh`).
- **Tidak mengaktifkan tulis alamat jaringan panel.** Itu jalur lain
  (`PANEL_NETWORK_WRITE_ENABLED` di backend).

Lisensi Inno Setup: `license.txt` memberi izin pemakaian termasuk komersial;
compiler menampilkan ajakan "purchase a license" — itu opsional (dukungan
pengembangan + bukti lisensi untuk audit internal).

## 7. Troubleshooting

| Gejala | Sebab / tindakan |
|---|---|
| Task `ZkPushAgent` langsung berhenti, `agent.log` bertambah tiap menit | Token kosong (exit `2`) atau DLL gagal dimuat (exit `3`). Baca log; jalankan `check_agent.ps1`. |
| `PullLastError=-201` saat push pertama | DLL transport tidak bersebelahan. Pastikan semua `pl*.dll` ada di `{app}\sdk`; pilih ulang folder SDK lalu pasang ulang. |
| "Could not find module ... or one of its dependencies" | VC++ Redistributable x86 belum ada. Installer memasangnya; kalau gagal (kode error dicatat di `CHECK-REPORT.txt`), jalankan `vc_redist.x86.exe` manual lalu restart. |
| `/health` menjawab **401** | Token di backend berbeda dengan `ZK_AGENT`/`ZK_PUSH_AGENT_TOKEN` mesin ini. Samakan (`BACKEND-SNIPPET.txt`) lalu restart agen dan backend. |
| `/health` tidak menjawab, port dipakai proses lain | Ada agen lain di port itu. Hentikan yang lama, atau pasang ulang dengan `/PORT=` lain (dan sesuaikan `PUSH_AGENT_URL`). |
| Backend di mesin lain: `503`/timeout | Bind host masih `127.0.0.1`. Jalankan `check_agent.ps1`; kalau perlu pasang ulang dengan `/HOST=0.0.0.0` (instaler membuka firewall untuk subnet lokal). |
| Push jalan tapi pintu tidak terbuka | Bukan masalah agen. Lihat jebakan "kartu tidak membuka pintu" di `../../.github/instructions/panel-write.instructions.md`. |
| Agen jalan di port/DLL yang berbeda dari yang dipasang | Environment variable `ZK_*` lama masih ada di PC itu. `setx` menulis level **User**, dan level User **menang** atas level Machine - jadi shell milik user tersebut tetap memakai konfigurasi lama, begitu juga proses apa pun yang dijalankan manual dari shell itu. `check_agent.ps1` menandai ini di laporan (`ZK_* ini usang dan MENANG di shell user`). Hapus yang usang: `[Environment]::SetEnvironmentVariable('ZK_PUSH_AGENT_PORT', $null, 'User')` (ulangi untuk `ZK_PULLSDK_DLL`, `ZK_PUSH_AGENT_TOKEN`), lalu jalankan agen lewat task. Task berjalan sebagai SYSTEM dan membaca level Machine, jadi task tidak terpengaruh. |

## 8. Status verifikasi

Sudah dibuktikan di host dev (Windows, tanpa hak admin):

- `build_installer.ps1` mengompilasi `.iss` tanpa error/peringatan; hasil
  `dist\ZkPushAgent-Setup-*.exe` ~21 MB.
- `smoke_test.ps1` - **27 pemeriksaan, semuanya OK**: pemasangan senyap keluar
  dengan kode `0`; seluruh berkas terpasang termasuk `sdk\plcommpro.dll`; laporan
  memuat `OK GET /health -> {"ok": true, "python_bits": 32}` dan baris `VERDICT`;
  `BACKEND-SNIPPET.txt` memuat URL (dengan port uji) + token yang benar; tidak ada
  proses tertinggal di port uji; uninstaller menghapus folder aplikasi **termasuk
  `sdk\`**.
- Agen yang dijalankan jalur itu memakai **port dan DLL hasil pemasangan**
  (`...\app\sdk\plcommpro.dll`), bukan nilai `ZK_*` milik shell - lihat bug 4.
- Command line yang dipakai Task Scheduler (`cmd.exe` dengan argumen
  `/c ""<folder>\run_agent.cmd""`) dijalankan apa adanya lewat
  `ProcessStartInfo` dari folder yang **berisi spasi** - berhasil, `/health`
  menjawab. Ini bagian yang paling mudah salah dan tidak bisa diuji dari
  pendaftaran task saja.
- `install_task.ps1` menolak tanpa Administrator dengan pesan jelas; `-Unregister`
  pada task yang tidak ada keluar `0`.
- `gen_token.ps1` menghasilkan token 30 karakter `[0-9a-z]` yang selalu berbeda.
- `tests/test_agent_installer.py` mengunci kontrak env var, pengaturan task,
  ASCII/BOM, larangan menulis ke panel, larangan mengemas SDK, larangan `{app}` di
  kode wizard awal, larangan kurung kurawal bersarang di komentar Pascal, dan
  larangan dialog tanpa penjaga `WizardSilent`.

**Empat bug nyata ditemukan oleh uji-uji ini, bukan oleh pembacaan kode:**

1. `FindSdkFolder()` memakai `ExpandConstant('{app}')` selama `InitializeWizard`:
   installer mati dengan *attempt to expand the "app" constant before it was
   initialized* sebelum halaman pertama muncul (dua dialog error muncul sekaligus
   di PC operator).
2. `{app}` di dalam komentar Pascal `{ ... }` menutup komentar lebih awal -
   ISCC berhenti dengan `String error` di baris itu.
3. `MsgBox` di halaman akhir tidak dijaga `WizardSilent`: dengan `/VERYSILENT`
   setup **tidak pernah keluar**, sehingga pemasangan massal akan menggantung di
   setiap PC yang verdictnya bukan OK.
4. `check_agent.ps1` membiarkan anak prosesnya (launcher, python agen) mewarisi
   `ZK_*` milik shell yang menjalankan installer. Level **User** menang atas level
   Machine, jadi agen pemeriksaan start di port 8081 dengan DLL `C:\PullSDK`
   padahal installer memasang port 8099 dan DLL di folder aplikasi. Sekarang
   `check_agent.ps1` memaksa nilai efektifnya dan melaporkan `ZK_*` usang.

Belum dijalankan di host ini (butuh Administrator): pendaftaran task sungguhan,
penulisan environment variable mesin, pembuatan aturan firewall, dan uninstaller
pada pemasangan level mesin. Setelah pemasangan pertama di PC target, simpan
`CHECK-REPORT.txt` sebagai catatan.
- `gen_token.ps1` menghasilkan token 30 karakter `[0-9a-z]` yang selalu berbeda.
- `tests/test_agent_installer.py` mengunci kontrak env var, pengaturan task,
  ASCII/BOM, larangan menulis ke panel, dan larangan mengemas SDK.

Belum dijalankan end-to-end di host dev (butuh Administrator, dan port 8081 di
host itu sedang dipakai agen manual): pendaftaran task sungguhan, penulisan
environment variable mesin, pembuatan aturan firewall, dan uninstaller. Setelah
pemasangan pertama di PC target, simpan `CHECK-REPORT.txt` sebagai catatan.
