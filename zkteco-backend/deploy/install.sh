#!/usr/bin/env bash
#
# Bare-metal installer for the ZKTeco C3 backend.
# Idempotent: safe to re-run for upgrades.
#
#   sudo bash deploy/install.sh
#
# Use `bash deploy/install.sh` rather than `./deploy/install.sh`: a bundle
# created on Windows loses the executable bit.
#
# Assumes the project has already been copied to $APP_DIR (default
# /opt/zkteco-backend), e.g. with:
#   rsync -av --exclude .venv --exclude __pycache__ ./zkteco-backend/ \
#         sentral@192.168.0.27:/tmp/zkteco-backend/
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/zkteco-backend}"
APP_USER="${APP_USER:-zkteco}"
VENV="$APP_DIR/.venv"
SERVICE_USER_CREATED=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Jalankan sebagai root (sudo)."

log "Memeriksa prasyarat"
command -v python3 >/dev/null || die "python3 tidak ditemukan. Install: apt install python3 python3-venv python3-pip"
python3 - <<'PY' || die "Butuh Python >= 3.10"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
python3 -c 'import venv' 2>/dev/null || die "Modul venv hilang. Install: apt install python3-venv"

[[ -d "$APP_DIR" ]] || die "Direktori $APP_DIR tidak ada. Copy project-nya dulu."

log "Membuat user sistem '$APP_USER' (jika belum ada)"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
    SERVICE_USER_CREATED=1
fi

log "Menyiapkan virtualenv"
if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"

log "Menyiapkan file .env"
if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    warn "File .env dibuat dari contoh. WAJIB diedit sebelum start:"
    warn "  - DATABASE_URL  (PostgreSQL, bukan db:5432)"
    warn "  - SCHEDULER_ENABLED dibiarkan false; unit worker yang menyalakannya"
else
    log ".env sudah ada, tidak diubah"
fi

log "Menjalankan migrasi database"
( cd "$APP_DIR" && set -a && . ./.env && set +a && "$VENV/bin/python" -m alembic upgrade head )

log "Memasang unit systemd"
install -m 0644 "$APP_DIR/deploy/zkteco-api.service"    /etc/systemd/system/zkteco-api.service
install -m 0644 "$APP_DIR/deploy/zkteco-worker.service" /etc/systemd/system/zkteco-worker.service

# Grant the service user ownership of files it needs to write at runtime.
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 0640 "$APP_DIR/.env"

systemctl daemon-reload

log "Mengaktifkan service"
systemctl enable --now zkteco-api.service
systemctl enable --now zkteco-worker.service

sleep 3
log "Status"
systemctl --no-pager --lines=0 status zkteco-api.service    || true
systemctl --no-pager --lines=0 status zkteco-worker.service || true

log "Uji endpoint lokal"
if curl -fsS --max-time 10 http://127.0.0.1:8000/health; then
    echo
    log "API merespons."
else
    warn "API belum merespons. Cek: journalctl -u zkteco-api -n 50 --no-pager"
fi

if [[ $SERVICE_USER_CREATED -eq 1 ]]; then
    log "Catatan: user '$APP_USER' baru dibuat tanpa login shell (sengaja)."
fi

cat <<'EOF'

Langkah lanjutan:
  1. Pre-flight konektivitas panel (WAJIB, dari host ini):
       cd /opt/zkteco-backend
       .venv/bin/python -m scripts.check_panels --csv devices_input.csv
     Kalau 0/23 terjangkau -> host ini tidak punya rute ke 10.100.1.x.
  2. Impor daftar panel:
       .venv/bin/python -m scripts.import_devices_csv devices_input.csv
  3. Reverse proxy: lihat deploy/nginx-zkteco.conf
EOF