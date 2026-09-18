#!/usr/bin/env bash
# Pre-flight TANPA MENGUBAH APA PUN di 192.168.0.27 (semua perintah read-only).
# Tujuan: tahu apakah Docker deploy layak, dan apa yang TIDAK boleh disentuh.
set -u

echo "=== 1. HOST ==="
uptime
head -3 /etc/os-release 2>/dev/null
echo "kernel: $(uname -r)  arch: $(uname -m)"

echo
echo "=== 2. HAK AKSES ==="
echo "user   : $(whoami)"
if sudo -n true 2>/dev/null; then echo "sudo   : TANPA SANDI (bisa)"; else echo "sudo   : MINTA SANDI (butuh manusia)"; fi
for d in /opt /srv /var/lib/docker "$HOME"; do
  if [ -w "$d" ]; then echo "tulis  : $d OK"; else echo "tulis  : $d TIDAK"; fi
done

echo
echo "=== 3. DOCKER ==="
command -v docker >/dev/null 2>&1 && echo "docker : $(docker --version 2>&1)" || echo "docker : TIDAK ADA"
docker compose version 2>&1 | head -1
docker ps -a --format '{{.Names}}  {{.Image}}  {{.Status}}  {{.Ports}}' 2>&1 | head -20
echo "container jalan: $(docker ps -q 2>/dev/null | wc -l)"
echo "service     : $(systemctl is-active docker 2>&1)"
echo "volume      : $(docker volume ls -q 2>/dev/null | wc -l)"

echo
echo "=== 4. PORT YANG DIPAKAI (jangan diganggu) ==="
ss -tlnp 2>/dev/null | sed -n '1p;/:(80|443|8000|5432|8080)\b/p' || ss -tln 2>/dev/null | head -25
echo "--- semua listener ---"
ss -tln 2>/dev/null | awk 'NR>1{print $4}' | sort -u | tr '\n' ' '
echo

echo
echo "=== 5. WEB SERVER (situs yang sedang jalan) ==="
ls -1 /etc/nginx/sites-enabled/ 2>/dev/null || echo "(tidak ada nginx sites-enabled)"
grep -rhE '^\s*(server_name|listen)' /etc/nginx/sites-enabled/ 2>/dev/null | sort -u | head -20
ls -1 /etc/apache2/sites-enabled/ 2>/dev/null || echo "(tidak ada apache sites-enabled)"

echo
echo "=== 6. JARINGAN ==="
ip -4 -o addr show scope global | awk '{print $2, $4}'
echo "gateway: $(ip route show default)"
echo "route ke panel 10.100.1.3:"
ip route get 10.100.1.3 2>&1 | head -2

echo
echo "=== 7. GATE UTAMA: PANEL ZKTeco (TCP 4370) ==="
ok=0; tot=0
for ip in 10.100.1.3 10.100.1.12 10.100.1.21 10.100.1.100; do
  tot=$((tot+1))
  if timeout 6 bash -c "</dev/tcp/$ip/4370" 2>/dev/null; then echo "  $ip:4370  TERBUKA"; ok=$((ok+1)); else echo "  $ip:4370  TIDAK BISA"; fi
done
echo "hasil: $ok/$tot"

echo
echo "=== 8. PUSH AGENT (jalur TULIS ke panel) ==="
if timeout 6 bash -c "</dev/tcp/10.100.1.100/8081" 2>/dev/null; then echo "  10.100.1.100:8081 TERBUKA"; else echo "  10.100.1.100:8081 TIDAK BISA"; fi

echo
echo "=== 9. SUMBER DAYA ==="
free -h | head -2
df -h / /var /opt 2>/dev/null | sort -u

echo
echo "PREFLIGHT_SELESAI"
