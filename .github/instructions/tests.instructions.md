---
description: "Use when writing, editing, or running pytest tests in this repo (tests/, conftest.py, fakes.py, pyproject.toml) — including tests for the dashboard wiring and the push endpoints. Cara memasang device palsu dan larangan menyentuh hardware atau DB nyata."
applyTo: ["**/tests/**", "**/pyproject.toml"]
---
# Tes (pytest)

```powershell
$PY = "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe"
& $PY -m pytest -q --rootdir "C:\laragon\www\zkteco\zkteco-backend" "C:\laragon\www\zkteco\zkteco-backend\tests"
```

- `--rootdir` penting: `pyproject.toml` memakai `testpaths=["tests"]` dan `pythonpath=["."]`, jadi tes berjalan relatif ke `zkteco-backend`.
- **Tes tidak boleh butuh hardware.** Device palsu dipasang lewat seam `app.services.device_client.set_client_factory(FakeDeviceClient)` (`tests/fakes.py`). Kalau tes Anda butuh panel sungguhan, itu salah.
- Tes juga **tidak boleh** menyentuh `live_check.db` atau server yang sedang dipakai user. Untuk tes UI/HTTP, jalankan **instance uji di port lain** dengan `PUSH_AGENT_URL` **unset** (permintaan yang lolos paling banyak menjawab 503) — lihat `dashboard-ui.instructions.md`.
- **Jumlah tes itu volatile** → jangan menulis angkanya di dokumen atau komentar; jalankan saja.
- Setiap perbaikan bug = satu tes yang mengunci perilakunya (pola: `tests/test_personnel_group_flow.py`, `tests/test_panel_push.py`, `tests/test_device_client.py`).
- Perubahan UI dashboard biasanya butuh tes wiring di `tests/test_api.py` yang memeriksa isi HTML/JS (mis. save dipasangkan dengan push, level menawarkan kirim, `askConfirm` dipakai alih-alih `confirm()`).
- Sebelum melapor: pytest lulus **dan** `ruff check` + `ruff format --check` bersih (§2 `AGENTS.md`).
