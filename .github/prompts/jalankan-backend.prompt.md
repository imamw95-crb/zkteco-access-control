---
description: "Nyalakan backend lokal (proses latar + auto-restart) dan pastikan /health menjawab."
name: "jalankan-backend"
agent: "agent"
---
1. Jalankan task VS Code **"Jalankan backend (hidup di latar)"** (setara `.\jalankan-backend.bat`).
2. Verifikasi dengan permintaan HTTP ke `http://localhost:8000/health` — **localhost**, bukan `127.0.0.1`.
3. Kalau gagal: baca 15 baris terakhir `zkteco-backend\logs\backend.log` dan `supervisor.log`, lalu laporkan penyebabnya dalam 3 baris.

Jangan menyalakan instance kedua di port 8000. Ingat: perubahan `app/static/dashboard.html` hanya
terlihat setelah restart (atau lewat reload otomatis tombol dev).
