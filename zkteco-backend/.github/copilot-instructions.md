# Copilot / AI agent instructions — zkteco-backend

**Kontraknya ada di `../AGENTS.md`** (root workspace `c:\laragon\www\zkteco`), dan file itu
selalu dimuat. File ini sengaja dibuat sangat pendek supaya **tidak menduplikasi** konten yang
sama — duplikasi berarti token terbuang di setiap permintaan.

- `../AGENTS.md` §0 = disiplin token + shortcut chat · §1 = isi workspace · §2 = perintah ·
  §3 = 15 aturan emas · §5 = peta instruksi on-demand · §6 = klaim usang.
- Detail on-demand: `../.github/instructions/*.instructions.md` — panel write · dashboard UI ·
  migrasi · tes · perintah dev · ZKAccess & data.
- Produk: backend pengganti **ZKAccess 3.5** untuk 23 panel ZKTeco C3 (`10.100.1.x`, TCP 4370);
  FastAPI + SQLAlchemy 2.0 + Alembic + APScheduler; UI di `app/static/dashboard.html`;
  Windows push agent di `agent/`.
- Tiga hal yang paling sering salah: (1) tes **tanpa hardware**; (2) baca = `zkaccess-c3`
  in-process, tulis = lewat agent Windows (DLL 32-bit — jangan pernah dimuat di backend);
  (3) **jangan menulis ke panel nyata tanpa izin user** (`dry_run` dulu).
- Jangan menulis jumlah tes / jumlah baris DB di dokumen — angka itu basi; jalankan perintahnya.

