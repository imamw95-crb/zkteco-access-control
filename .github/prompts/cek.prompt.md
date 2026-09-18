---
description: "Jalankan pytest + ruff pada zkteco-backend dan laporkan hasilnya ringkas (tanpa dump output)."
name: "cek"
agent: "agent"
argument-hint: "(opsional) nama file/tes yang difokuskan"
---
Jalankan tes dan lint, lalu laporkan **ringkas**.

```powershell
$PY = "C:\laragon\www\zkteco\zkteco-backend\.venv\Scripts\python.exe"
& $PY -m pytest -q --rootdir "C:\laragon\www\zkteco\zkteco-backend" "C:\laragon\www\zkteco\zkteco-backend\tests"
& $PY -m ruff check "C:\laragon\www\zkteco\zkteco-backend"
& $PY -m ruff format --check "C:\laragon\www\zkteco\zkteco-backend"
```

Kalau ada `${input:scope}`, fokuskan ke situ (mis. satu file tes).

Laporan **maksimal 6 baris**: jumlah lulus/gagal · daftar `failure` (nama tes + penyebab 1 baris) ·
status ruff (jumlah temuan). Jangan menempel output penuh dan jangan membuka file kecuali ada
kegagalan yang perlu didiagnosis.
