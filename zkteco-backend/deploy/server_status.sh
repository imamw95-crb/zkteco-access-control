#!/usr/bin/env bash
# Status singkat deploy docker di server ini.
#
# Nilai rahasia (sandi, token) TIDAK pernah dicetak — hanya jumlah karakternya.
# Aman dijalankan kapan saja; tidak mengubah apa pun.
set -eu

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

echo "=== .env ($APP_DIR/.env) ==="
if [ -f .env ]; then
  stat -c '  mode %a, %s byte' .env
  while IFS='=' read -r key value; do
    [ -z "$key" ] && continue
    case "$key" in \#*) continue ;; esac
    case "$key" in
      *PASSWORD*|*TOKEN*|*SECRET*) printf '  %-28s <%s karakter, disembunyikan>\n' "$key" "${#value}" ;;
      *) printf '  %-28s %s\n' "$key" "$(printf '%s' "$value" | sed -E 's#://([^:]+):[^@]*@#://\1:***@#')" ;;
    esac
  done < .env
else
  echo "  BELUM ADA — jalankan deploy/server_setup_env.sh"
fi

echo
echo "=== agen push Windows (jalur TULIS ke panel) ==="
url="$(sed -n 's/^PUSH_AGENT_URL=//p' .env 2>/dev/null | tail -1)"
token="$(sed -n 's/^PUSH_AGENT_TOKEN=//p' .env 2>/dev/null | tail -1)"
if [ -z "$url" ]; then
  echo "  PUSH_AGENT_URL kosong -> setiap push akan menjawab 503"
else
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -H "Authorization: Bearer $token" "$url/health" || echo 000)"
  echo "  $url/health -> HTTP $code"
  case "$code" in
    200) echo "  token DITERIMA oleh agen" ;;
    401) echo "  token DITOLAK (401) — samakan dengan token agen" ;;
    000) echo "  agen tidak menjawab" ;;
  esac
fi

echo
echo "=== container ==="
if docker compose ps >/dev/null 2>&1; then
  docker compose ps
else
  echo "  (belum bisa akses docker: $(docker compose ps 2>&1 | head -1))"
fi
