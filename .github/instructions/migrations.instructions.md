---
description: "Use when changing the database schema, adding or editing Alembic revisions, or running alembic upgrade/heads/stamp. Aturan rantai migrasi, batch mode SQLite, dan jebakan delete/parent."
applyTo: ["**/migrations/**", "**/alembic.ini", "**/app/models.py", "**/app/database.py"]
---
# Migrasi (Alembic)

- **Alembic = sumber kebenaran skema.** `Base.metadata.create_all` hanya untuk DB yang dibangun dari nol, bukan jalur perubahan skema.
- Rantai **linear, satu head**: `795ec8945bac` → `39d8af1b06df` → `907835756e6e` → `69f13e9af7f0` → `a1d4c7b90e35`. Cek: `python -m alembic heads` (jalankan dari `zkteco-backend/`).
- **Jangan mengedit revisi yang sudah ada** kalau sudah pernah di-`upgrade` di DB mana pun — buat revisi baru, jangan tulis ulang sejarah.
- Perubahan/penghapusan kolom di SQLite wajib lewat `batch_alter_table`.
- Migrasi data + DDL dalam satu revisi: promosikan nilai dulu, tautkan, **baru** drop kolom (lihat `69f13e9af7f0` untuk departemen). Uji **upgrade DAN downgrade**, pastikan data selamat (NULL/whitespace → NULL).
- **Jebakan:** SQLAlchemy melepas child dari parent yang dihapus dengan mengeset FK ke NULL — ini **membatalkan** re-parent yang dilakukan pada flush yang sama. Perbaikan ada di `DepartmentService.delete`: bulk `UPDATE` child + `COMMIT`, lalu hapus di langkah kedua.
- Seeding idempoten ada di `app/main.py` lifespan (`_seed_presets()`), supaya DB hasil `create_all` tetap lengkap (mis. zona "24 Jam").
- Perubahan skema = revisi baru + **tes**; perbarui `README.md`/`AGENTS.md` di commit yang sama kalau perilaku yang didokumentasikan ikut berubah.
