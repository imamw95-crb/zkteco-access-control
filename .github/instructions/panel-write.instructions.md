---
description: "Use when writing to, pushing to, reading, or revoking access on ZKTeco C3 panels: personnel sync, /api/personnel/sync, push_to_device, panel_push, carry_over, userauthorize, dry_run, panel network params, Windows push agent, plcommpro. Berisi fakta hardware yang sudah diverifikasi dan jebakan yang sudah memakan waktu."
applyTo: ["**/app/services/panel_push.py", "**/app/services/push_agent.py", "**/app/services/panel_network.py", "**/agent/**", "**/scripts/check_push_write.py", "**/scripts/verify_person_on_panels.py"]
---
# Menulis / membaca panel ZKTeco C3

Firmware semua panel: `AC Ver 5.4.3.2001`. 23 panel di `10.100.1.x`, TCP 4370.
**Fakta di bawah sudah diverifikasi di hardware nyata — jangan diperdebatkan lagi.**

## Yang sudah terbukti (jangan ulangi klaim lama)
- `zkaccess-c3` 0.0.15 **tidak bisa menulis** data user/timezone (perintah `0x07 SETDATA` memang tidak ada di library). Itu batas **library**, bukan batas panel.
- **Kontrol pintu JELAS BEKERJA**: `POST /api/control/devices/{id}/open` mengembalikan `200` dengan `success: true` di panel asli — `open_door`/`lock_status` memakai perintah **CONTROL**, mekanisme berbeda dari **SETDATA** yang hilang. Jangan ulangi klaim "kontrol pintu tidak mungkin". Catatan: endpoint ini membalas **200 dengan `{success:false}`** kalau panel gagal → periksa `result.success`, bukan status HTTP.
- **Jalur tulis terbukti**: backend → agent Windows → `plcommpro.dll` → panel. Uji tulis+hapus user di panel `RUANGAN SERVER` berhasil dengan daftar PIN identik sebelum/sesudah.
- Jumlah personel nyata = membaca field `UID` pada tabel `user` (`~MaxUserCount` hanya kapasitas).
- Jaringan panel: `IPAddress`, `NetMask`, `MAC` terbaca di kedua jalur (library `c3` maupun SDK). **`GATEWAY` TIDAK terbaca** di firmware ini — `c3` menghilangkan key-nya, agent menjawab string kosong (terverifikasi `10.100.1.5`). Rencana menulis IP panel **wajib meminta netmask+gateway dari operator**, bukan menyalin dari bacaan.

## Jalur tulis (agent Windows)
- `plcommpro.dll` **32-bit** → agent butuh Python **32-bit** sendiri. Backend 64-bit **jangan pernah** mencoba memuat DLL itu.
- **Jebakan `-201`**: DLL ter-load dan panel terjangkau, tapi `Connect()` gagal `-201` karena SDK mencari DLL transport relatif ke **working directory**. `os.add_dll_directory()` TIDAK cukup; yang bekerja `os.chdir(sdk_dir)` — lihat `prepare_sdk_folder()` di `agent/zk_push_agent.py`. `agent/check_setup.py --test <ip>` ada untuk menangkap ini sebelum push.
- Kode error **transien**: `-2` (sibuk), `-107` (koneksi ditolak), `-112` (balasan > buffer 64 KB). **Baca ulang, jangan simpulkan panel mati. Kegagalan baca ≠ kegagalan tulis.**
- **`transaction` TIDAK bisa dibaca multi-field** di firmware ini: SDK menjawab `-2`, library menjawab `Wrong table returned by panel` — untuk 2 field maupun `*`. Satu field dilayani normal (terukur 2026-09-18 di `10.100.1.3`: `Pin` → 56.960 baris dalam 8,6 detik; 2/4/9-field ditolak). Karena itu `LogService._read_transaction_fields()` membaca **satu field per request** lalu menyusun ulang **per posisi baris** — dan **menolak menyimpan** kalau jumlah baris antar field berbeda. Penolakan itu disengaja: Pin dari satu bacaan ditempel ke Cardno dari bacaan lain = log akses yang salah, lebih buruk daripada pull yang gagal. Jangan "perbaiki" dengan mengirim banyak field sekaligus.
- Buffer agent (`ZK_AGENT_BUFFER_SIZE`, default 64 KB) juga harus dinaikkan: satu field untuk 57 ribu baris ≈ 0,5 MB, dan balasan lebih besar dari buffer gagal. Set `4194304` lalu **restart agent** (env dibaca saat start).
- `LogService._read_transactions()` mencoba **agent lebih dulu**, lalu library — **jangan dibalik urutannya**. Pesan error membedakan penyebab: `-112` = buffer, `-2`/`-107` = sibuk/timeout (ulangi; panel sehat). Keduanya pernah tertukar dan sempat menyesatkan diagnosa.
- Endpoint agent (Bearer token, menolak start tanpa `ZK_PUSH_AGENT_TOKEN`): `GET|POST /panels/{ip}/tables/{table}` (body `{"records":[...]}`, `{"delete": true}` untuk hapus), `/panels/{ip}/params`, `/panels/{ip}/control`, `/health`.

## Aturan push
- `SetDeviceData` **MENGGANTI** record, bukan menambal: field yang tidak dikirim **dikosongkan**. Terbukti di hardware (menulis ulang user `Fega` menghapus `Password=123456`). Karena itu `panel_push._carry_over()` menyalin nilai panel sendiri untuk `Password, Group, StartTime, EndTime, SuperAuthorize, Disable` ke setiap record yang ditulis ulang — **jangan hapus mekanisme ini**.
- **Diff-based**: `push_to_device` membaca panel dulu; kalau panel tidak terbaca → **tidak menulis apa pun** (jangan jatuh ke mode "kirim semua").
- Baris `user` **tidak pernah dihapus** (hapus+add kehilangan field yang tidak kita kirim). Pencabutan hak = hapus baris `userauthorize`; `revoke_pins` menyapu **semua** device, bukan hanya yang tercakup.
- Push dengan `personnel_ids` = **otoritatif** (hak lama dicabut). Push se-panel = **aditif** (panel menyimpan user yang tidak kita ketahui; mencabutnya akan mengunci orang lain keluar).
- **`OK PETUGAS` istimewa**: panel memegang **405** user padahal hanya **361** yang bisa diturunkan dari level kita (44 tak terjelaskan), dan serial/LockCount-nya tidak terbaca software ini. **Selidiki sebelum push ke panel itu.**
- Sapuan armada dilaporkan **per panel** lewat `POST /api/personnel/sync?stream=true` (NDJSON, satu baris per panel + baris ringkasan terakhir). Jalur tanpa `stream` dipakai skrip — **jangan ubah bentuk balasannya**.
- Wajib sebelum uji tulis baru: `dry_run=true` → lapor → **izin user** → satu panel uji → verifikasi (`scripts/verify_person_on_panels.py`) → baru meluas.

## Pemetaan model ↔ tabel panel (dipakai saat push)
| Tabel/field panel | Model kita |
|---|---|
| `user.Pin` | `Personnel.employee_id` |
| `user.CardNo` | `Personnel.card_number` |
| `userauthorize.Pin` | `Personnel.employee_id` |
| `userauthorize.AuthorizeTimezoneId` | `AccessGroup.device_timezone_id` |
| `userauthorize.AuthorizeDoorId` (bitmask `1 << (pintu-1)`) | `access_group_doors` |
| satu baris `userauthorize` per keanggotaan | `personnel_access_groups` |
| `timezone.SunTime1..SatTime1` (3 segmen) | `AccessTimeZoneSlot` (hari **0=Minggu**) |

`user`/`userauthorize`/`timezone` **tidak punya kolom device** → datanya per panel, jadi push
selalu dihitung per device. `DoorSchedule` berbasis Senin vs `AccessTimeZoneSlot` berbasis
Minggu = **disengaja**. `AuthorizeDoorId` = 4 bit (pintu 1..4 di panel itu).

## Jebakan #1 di lapangan: kartu tidak membuka pintu
Nomor yang **tercetak di kartu** biasanya **BUKAN** nilai Wiegand yang dibaca reader.
`SetDeviceData` menulis angka salah dengan setia, tanpa error, dan pintu tidak pernah terbuka.
- Diagnosis lewat tabel `transaction`: baris `Pin=0` + `EventType=27` = kartu tidak dikenal, `CardNo` di baris itu = angka yang benar-benar dibaca. `EventType=0` = diterima.
- **Tidak ada baris sama sekali** = panel tidak pernah membaca angka itu.
- Record dashboard hanya sampai ke panel kalau **di-push** — orang uji yang belum di-push gagal dengan gejala identik.
- Simpan nomor cetak di `Name`; itu satu-satunya label yang dibawa kartu.

## Tulis alamat jaringan panel (`POST /api/devices/{id}/network`) — default OFF
`app/services/panel_network.py`. Tanpa `PANEL_NETWORK_WRITE_ENABLED=true`: `?dry_run=false` →
**503**; dry run tetap `200` (`enabled:false`) dan **tidak pernah memanggil agent**.
Pengaman yang **wajib dipertahankan**: IP harus di dalam `SCAN_ALLOWED_NETWORKS`, serial panel
dicek dulu, MAC dibawa apa adanya (4 param dikirim sekali — apakah `SetDeviceParam` menghapus
param yang tidak dikirim **belum diketahui**), `dry_run` default `true`, UI mengunci tombol tulis
sampai nilai yang sama di-dry-run, dan record dashboard **hanya** diperbarui kalau alamat BARU
menjawab. Alamat salah = panel tidak terjangkau **tanpa jalan pulang** (harus diperbaiki fisik),
dan `GATEWAY` tidak bisa dibaca untuk disalin. Jalur aman yang tetap ada: perbaiki record di
dashboard (tab Cari Device — hanya menulis DB), atau ubah alamat di panel lebih dulu lalu samakan
recordnya. Daftar pengaman lengkap: `README.md` §"Changing a device's IP".
