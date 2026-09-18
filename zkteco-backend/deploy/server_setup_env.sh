#!/usr/bin/env bash
# Buat berkas `.env` untuk stack docker compose di server deploy.
#
# Sengaja dijalankan DI SERVER: sandi PostgreSQL dibuat acak dengan `openssl` dan
# ditulis langsung ke berkas — nilainya tidak pernah lewat chat, terminal, atau
# riwayat perintah.
#
# Aman diulang: kalau `.env` sudah ada, berkas itu tidak akan ditimpa.
set -eu

# Berkas ini ada di deploy/, tetapi .env harus berada di akar proyek — di situlah
# `docker compose` dan `server_status.sh` mencarinya.
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  echo ".env sudah ada - tidak ditimpa"
  exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl tidak ada" >&2
  exit 1
fi

PW="$(openssl rand -hex 16)"
umask 077

cat > .env <<EOF
# Dibuat oleh deploy/server_setup_env.sh pada $(date -Is)
# Password di bawah dibuat acak di server ini.

# --- database (dipakai docker compose untuk service "db") --------------------
POSTGRES_USER=zkteco
POSTGRES_PASSWORD=$PW
POSTGRES_DB=zkteco
# Hanya dipakai kalau perintah dijalankan di luar compose; compose menimpanya
# dengan alamat service "db".
DATABASE_URL=postgresql+psycopg://zkteco:$PW@db:5432/zkteco

# --- komunikasi panel --------------------------------------------------------
DEVICE_PORT=4370
DEVICE_PARALLELISM=8

# --- scheduler (hanya hidup di container "worker") --------------------------
SCHEDULER_ENABLED=false
LOG_POLL_INTERVAL_SECONDS=60
HEALTH_CHECK_INTERVAL_SECONDS=120

# --- agen push Windows (jalur TULIS ke panel) --------------------------------
PUSH_AGENT_URL=http://10.100.1.100:8081
# PUSH_AGENT_TOKEN tidak ditulis di sini: barisnya ditambahkan terpisah supaya
# nilainya tidak pernah muncul di berkas ini dua kali atau di riwayat perintah.
# Tanpa token, setiap push menjawab 503 dan tidak ada yang ditulis ke panel.

# --- tab "Cari Device" -------------------------------------------------------
SCAN_ALLOWED_NETWORKS=10.100.1.0/24
SCAN_MAX_HOSTS=4096

# --- dashboard ---------------------------------------------------------------
# Situs ini diakses lewat http:// di LAN, jadi cookie sesi tidak boleh secure.
AUTH_COOKIE_SECURE=false
AUTH_SESSION_HOURS=12
LOG_LEVEL=INFO
PANEL_NETWORK_WRITE_ENABLED=false
EOF

chmod 600 .env
echo ".env dibuat (mode 600). Kunci yang ada:"
grep -E '^[A-Z_]+=' .env | cut -d= -f1 | sed 's/^/  /'
