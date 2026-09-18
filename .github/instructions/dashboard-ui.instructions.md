---
description: "Use when editing the dashboard UI (app/static/dashboard.html), adding buttons, forms, modals or tabs, wiring a save/push flow, or testing the UI with Playwright/browser tools. Berisi insiden nyata yang pernah merusak data produksi."
applyTo: ["**/app/static/**", "**/app/api/dashboard.py", "**/tests/test_api.py"]
---
# Dashboard UI (`app/static/dashboard.html`)

Satu file, vanilla JS, **tanpa build step**. `app/main.py` membaca HTML-nya **saat import** →
**RESTART server** untuk melihat perubahan (tombol dev sudah memakai `--reload-include *.html`).

## Wajib
- **JANGAN pakai `confirm()` bawaan browser.** Pakai `askConfirm({title, detail, confirmLabel})`
  (mengembalikan `Promise<boolean>`, menggambar `.modal-overlay`/`.modal`). Alasan nyata: begitu
  browser berhenti menampilkan dialog native (Chrome "prevent additional dialogs", webview),
  `confirm()` mengembalikan **false tanpa dialog** → operator hanya melihat "Dibatalkan" dan
  mengira fitur rusak. Escape / klik backdrop / "Batal" = batal.
- Menyimpan personel **juga menulis ke panel** (checkbox `#p-sync-auto`, **default aktif**,
  di-reset oleh `resetPersonnelForm()`). Kalau dimatikan, pesannya harus eksplisit
  **"PANEL BELUM DIUBAH"** — nomor kartu yang belum terbukti di reader akan tersebar setia ke
  23 panel dan pintunya tidak pernah terbuka.
- Hasil push **tidak boleh** disederhanakan jadi "selesai": sapuan melewati 23 panel yang bisa
  gagal sendiri-sendiri, dan panel yang gagal **masih memegang data lama**. Pakai `pushOutcome()`
  → `"N panel GAGAL … MASIH memakai data lama"` (warna error).
- Progres per panel: `POST /api/personnel/sync?stream=true` (NDJSON) → `showPushProgress()`,
  `readPushStream()`, `panelLine()`. Popup tidak bisa ditutup sampai sapuan selesai, dan verdict
  yang sama tetap ada di `#p-msg`. Jalur non-stream dipakai skrip — **jangan ubah bentuk balasannya**.
- Ganti target edit **wajib** `askConfirm()` dan scroll reveal **instan** (`{block:'center'}` tanpa
  `behavior`). Alasan: form bersama menyimpan target di variabel; saat animasi scroll masih
  berjalan, klik bisa mendarat di baris lain → penyimpanan membajak record orang lain.
- Mengubah access level **tidak menulis ke panel** → setiap aksi harus menawarkan kirim dan bilang
  "PANEL BELUM DIUBAH" kalau operator menolak atau panelnya gagal:
  - tambah pintu → `pushPanel(deviceId)` untuk device itu;
  - tambah anggota → `pushEverywhere(ids, ..., {deviceIds})` **dibatasi ke device level itu**
    (jangan sebar ke 23 panel: panel level lain tidak berubah dan hanya menenggelamkan yang penting);
  - hapus anggota → sapuan **penuh** (`pushEverywhere([id])`) karena hak lama bisa tertinggal di
    panel mana pun.
- `POST /api/control/devices/{id}/open` membalas **200 dengan `{success:false}`** kalau panel gagal
  → periksa `result.success`, bukan status HTTP.

## Insiden yang sudah terjadi (jangan diulang)
- **2026-09-16 — penyimpanan menimpa data karyawan asli.** Simpan yang ditujukan ke record uji
  malah menimpa `id 266` (`10102045`, Slamet): nama, kartu, departemen, dan level aksesnya.
  Penyebab: form bersama + klik berbasis koordinat sesudah scroll. Dipulihkan dengan membaca balik
  ZKAccess (yang tetap jadi otoritas fallback).
- **UI test bisa menembus ke server nyata.** `page.route('**/api/personnel/sync**')` pernah
  **tidak** menangkap permintaan, sehingga sapuan scope nyata (`personnel_ids=116&device_ids=23`)
  benar-benar membaca panel `10.100.1.26`. Stub **bukan** jaring pengaman.
  **ATURAN:** setiap tes UI yang bisa memicu push harus berjalan di **instance uji (port lain)**
  dengan `PUSH_AGENT_URL` **unset**, sehingga permintaan yang lolos paling banyak menjawab **503**.
  Hitung jumlah request yang berhasil diintersep, dan grep log server sesudahnya.
- Playwright di tool ini: tombol dashboard butuh `dispatchEvent('click')` (klik biasa timeout);
  `removeAllListeners('dialog')` sebelum memasang handler dialog baru (kalau tidak, handler lama
  yang mencuri dialog); `setTimeout` **tidak ada** → gunakan `page.waitForTimeout(ms)`.

## Detail kecil yang mudah rusak
- **Tab default = Personel.** `<button class="tab active" data-tab="personnel">` harus sejalan
  dengan `hidden` di `#panel-*` (Personel tampil, Device `hidden`), dan boot memakai
  `selectTab('personnel')` — **satu** jalur dengan klik. Tombol yang menyala dan panel yang
  tampil berbeda = operator mengetik kartu ke form yang dia kira milik halaman lain.
  `loadDevices()` **tetap** dipanggil saat boot (mengisi select bersama: buka pintu, push panel,
  jaringan; tidak ada yang memuat ulang daftar device saat tab Device diklik).
- Empty state: `colspan` personel = **6**, device = **7**.
- Filter personel: `applyPersonnelFilter()` dipanggil di **akhir** `loadPersonnel()` (kalau tidak,
  filter tidak pernah diterapkan), dan kotak cari `select()` saat `focus` — harus **sinkron**
  (menunda dengan rAF/setTimeout menelan keystroke pertama).
- `say()`/`sayBusy()` membersihkan `el.busyTimer` (dulu bocor satu `setInterval` tiap simpan).
- `loadLevels()` mengisi dropdown jam akses dan **default ke zona `is_24_hour`** — level dengan
  jadwal terbatas bisa mengunci orang keluar dari pintu yang hari ini bisa mereka buka.
- Perubahan UI dikunci tes wiring di `tests/test_api.py` (mis. save+sync dipasangkan, level
  menawarkan kirim ke panel) — perbarui tesnya di perubahan yang sama.
