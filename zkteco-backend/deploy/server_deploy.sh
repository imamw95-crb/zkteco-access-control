#!/usr/bin/env bash
# Jalankan / perbarui stack docker compose di server deploy ini.
#
# TIDAK memakai sudo: user harus sudah ada di grup `docker`
# (sekali: sudo usermod -aG docker $USER, lalu login ulang).
#
#   bash deploy/server_deploy.sh                # aman: menolak menimpa data sungguhan
#   bash deploy/server_deploy.sh --force-copy   # timpa isi database dengan live_check.db
#
# Aman diulang. Urutan: build+up -> tunggu /health -> salin data -> verifikasi.
#
# CATATAN PENTING: `worker` (penjadwal: tarik log + health check 23 panel
# tiap 60-120 detik) TIDAK dinyalakan di sini. Panel C3 hanya menerima SATU
# koneksi sekaligus, dan backend dev di laptop masih hidup — dua penjadwal pada
# panel yang sama akan saling menendang. Nyalakan hanya setelah pindah penuh:
#   docker compose up -d worker
set -eu

FORCE_COPY=no
case "${1:-}" in
  --force-copy) FORCE_COPY=yes ;;
  "") ;;
  *) echo "argumen tidak dikenal: $1" >&2; exit 2 ;;
esac

APP="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP"

if ! docker info >/dev/null 2>&1; then
  echo "GAGAL: tidak bisa mengakses docker." >&2
  echo "Jalankan sekali:  sudo usermod -aG docker \$USER   lalu login ulang." >&2
  exit 1
fi

echo "=== 1. build + nyalakan (db, backend) ==="
docker compose up -d --build db backend

echo
echo "=== 2. tunggu /health ==="
ok=no
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    ok=yes
    break
  fi
  sleep 2
done
if [ "$ok" = yes ]; then
  echo "  /health: $(curl -sS --max-time 5 http://127.0.0.1:8000/health)"
else
  echo "  /health belum menjawab setelah 120 detik — log backend:"
  docker compose logs --tail 40 backend
  exit 1
fi

echo
echo "=== 3. status container ==="
docker compose ps

echo
echo "=== 4. salin data dari live_check.db ==="
if [ ! -f live_check.db ]; then
  echo "  live_check.db tidak ada di $APP — lewati (database dibiarkan seperti sekarang)."
else
  MOUNTS=(-v "$APP/live_check.db:/tmp/live_check.db:ro" -v "$APP/scripts:/srv/scripts:ro")

  # Aplikasi MENYEMAI data awalnya sendiri saat start pertama (time zone
  # "24 Jam" dan akun admin/hr), jadi tabel tujuan tidak pernah benar-benar
  # kosong. Yang menentukan boleh-tidaknya menimpa: apakah sudah ada data
  # SUNGGUHAN (panel, personel, riwayat sync) di sana.
  hitung() {
    docker compose exec -T db psql -U zkteco -d zkteco -tAc "$1" | tr -dc '0-9'
  }
  nyata="$(hitung "select (select count(*) from devices) + (select count(*) from personnel) + (select count(*) from sync_runs)")"
  nyata="${nyata:-1}"

  if [ "$FORCE_COPY" = yes ]; then
    force=yes
    echo "  --force-copy: tabel tujuan dikosongkan per tabel lalu diisi dari live_check.db."
  elif [ "$nyata" = 0 ]; then
    force=yes
    echo "  Database tujuan belum berisi data sungguhan (0 panel/personel/riwayat),"
    echo "  hanya data awal aplikasi — aman dikosongkan lalu diisi."
  else
    force=no
    echo "  Database tujuan SUDAH berisi data sungguhan ($nyata baris panel/personel/riwayat)."
    echo "  Penyalinan DILEWATI supaya tidak ada data yang tertimpa."
    echo "  Kalau memang ingin menimpanya:  bash deploy/server_deploy.sh --force-copy"
  fi

  if [ "$force" = yes ]; then
    echo "  -- dry run (hitung saja) --"
    docker compose run --rm "${MOUNTS[@]}" backend \
      python -m scripts.copy_sqlite_to_postgres --source /tmp/live_check.db --dry-run
    echo "  -- salin sungguhan --"
    docker compose run --rm "${MOUNTS[@]}" backend \
      python -m scripts.copy_sqlite_to_postgres --source /tmp/live_check.db --force
  fi
fi

echo
echo "=== 5. verifikasi isi database ==="
docker compose exec -T db psql -U zkteco -d zkteco -c "
  select (select count(*) from devices)                 as panel,
         (select count(*) from personnel)               as personel,
         (select count(*) from access_groups)           as level_akses,
         (select count(*) from personnel_access_groups) as hak_akses,
         (select count(*) from access_group_doors)      as pintu_level,
         (select count(*) from users)                    as pengguna;"

echo
echo "=== 5b. panel yang terdaftar (5 pertama) ==="
docker compose exec -T db psql -U zkteco -d zkteco -c \
  "select id, name, ip, port from devices order by id limit 5;"

echo
echo "=== 6. ringkasan ==="
bash "$APP/deploy/server_status.sh"

echo
echo "SELESAI."
echo "  Dashboard : http://192.168.0.27:8000/"
echo "  Login     : akun yang disalin dari laptop (tabel users diisi dari live_check.db)"
echo "  Penjadwal : belum dinyalakan — 'docker compose up -d worker' setelah pindah penuh"
echo "              dari laptop (panel hanya menerima satu koneksi sekaligus)."
