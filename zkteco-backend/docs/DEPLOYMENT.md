# Deployment runbook

## Recon already done (2026-09-16)

Target: `sentral@192.168.0.27` (SSH 22).

| Port | State | Note |
|---|---|---|
| 22 | open | SSH |
| 80 | open | **already serving another site** |
| 443 | open | **already serving another site** |
| 8000 | closed | free — use this for the API |
| 5432 | closed | no PostgreSQL yet — must be installed |

Ports 80/443 are taken, so do **not** replace the existing nginx vhost. Add a
separate `server_name`, or expose the API on 8000 and let the existing proxy
route to it. See [`deploy/nginx-zkteco.conf`](../deploy/nginx-zkteco.conf).

Panels were verified reachable **from the Windows dev host** (23/23 on TCP 4370).

## Step 0 — mandatory pre-flight on the target host

The whole product is useless if the server cannot open TCP 4370 to the panels.
Check this **before** installing anything:

```bash
# on 192.168.0.27, once the code is copied (step 2)
cd /opt/zkteco-backend
.venv/bin/python -m scripts.check_panels --csv devices_input.csv
```

Or check first, before copying anything:

```bash
python3 -m scripts.check_panels --csv devices_input.csv   # plain python also works
```

- `23/23 reachable` → proceed.
- `0/23 reachable` → **stop**. The host has no route to `10.100.1.0/24`. Fix
  routing/firewall, or run the backend on a host that does (e.g. the current
  Windows machine, which works).

## Step 1 — build the deployment bundle (on Windows)

```powershell
cd C:\laragon\www\zkteco\zkteco-backend
powershell -ExecutionPolicy Bypass -File .\deploy\make_bundle.ps1
```

Produces `deploy/zkteco-backend-<timestamp>.tar.gz` (~45 KB) containing `app/`,
`migrations/`, `scripts/`, `tests/`, `deploy/`, `requirements.txt`, plus the
panel list. `.venv`, `*.db`, `.env` and caches are excluded.

## Step 2 — copy to the server

```powershell
scp .\deploy\zkteco-backend-<timestamp>.tar.gz sentral@192.168.0.27:/tmp/
```

```bash
# on the server
sudo tar -xzf /tmp/zkteco-backend-<timestamp>.tar.gz -C /tmp
sudo rm -rf /opt/zkteco-backend
sudo mv /tmp/zkteco-backend /opt/zkteco-backend
```

## Step 3 — database

```bash
sudo apt update && sudo apt install -y postgresql
sudo -u postgres psql <<'SQL'
CREATE USER zkteco WITH PASSWORD 'CHANGE_ME';
CREATE DATABASE zkteco OWNER zkteco;
SQL
```

## Step 4 — configure and install

```bash
cd /opt/zkteco-backend
sudo cp .env.example .env
sudo nano .env
```

Set at minimum:

```ini
DATABASE_URL=postgresql+psycopg://zkteco:CHANGE_ME@localhost:5432/zkteco
SCHEDULER_ENABLED=false
LOG_LEVEL=INFO
```

Then run the installer (note: `bash`, not `./` — the tarball was built on
Windows and has no executable bit):

```bash
sudo bash deploy/install.sh
```

It creates the `zkteco` system user, builds `.venv`, installs requirements, runs
`alembic upgrade head`, installs both systemd units, enables and starts them, and
probes `/health` on 127.0.0.1:8000.

## Step 5 — load the panels and verify

```bash
cd /opt/zkteco-backend
.venv/bin/python -m scripts.check_panels --csv devices_input.csv     # gate: must be > 0/23
.venv/bin/python -m scripts.import_devices_csv devices_input.csv
.venv/bin/python -m scripts.live_check --csv devices_input.csv --limit 25
curl -s http://127.0.0.1:8000/api/dashboard/devices | head -c 400
```

Dashboard: <http://192.168.0.27:8000/> · Swagger: `/docs`

## Docker Compose — the path actually used (executed 2026-09-18)

Docker was **already installed** on `192.168.0.27` (29.4.3 + compose v5.1.3), so
nothing had to be installed. What was done:

```bash
# on the server, as user `sentral` (needs to be in the `docker` group once:
#   sudo usermod -aG docker $USER   -> re-login)
mkdir -p ~/zkteco-backend          # NOT /opt: that needs sudo on every command
# copy app/ migrations/ scripts/ alembic.ini pyproject.toml requirements.txt \
#      Dockerfile docker-compose.yml .env.example devices_input.csv
bash deploy/server_setup_env.sh    # writes .env with a random Postgres password
# then append PUSH_AGENT_TOKEN (see below), and:
bash deploy/server_deploy.sh
```

- Ports 80/443/8080/8081/3306/3000/888/9090 belong to other services on that
  host; only **8000** is published, and 5432 stays inside the compose network.
  `docker-compose.yml` therefore pins `name: zkteco` so its networks and volume
  cannot collide with the other compose project already running there.
- `deploy/server_status.sh` prints the `.env` with secrets masked, probes the
  Windows push agent `/health`, and shows `docker compose ps`.
- **Writing to panels needs `PUSH_AGENT_URL` + `PUSH_AGENT_TOKEN` inside the
  container** — that is why both `backend` and `worker` now list `env_file: .env`
  (matching `EnvironmentFile=` in the systemd units). Without it the container
  sees no agent and every push answers 503.
- Data can be loaded from the dev SQLite database with
  `scripts/copy_sqlite_to_postgres.py` (run inside the backend container with
  `-v $(pwd)/live_check.db:/tmp/live_check.db:ro -v $(pwd)/scripts:/srv/scripts:ro`).
  `deploy/server_deploy.sh` does this automatically, and **only** when the target
  still holds no real data (0 devices/personnel/sync_runs) — otherwise it refuses
  unless given `--force-copy`. The app seeds a default `access_time_zones` row and
  the `admin`/`hr` accounts on first start, so the target is never literally
  empty; the script treats "no devices/personnel/sync_runs" as "seed-only, safe".
- `worker` is deliberately **not** started while the Windows laptop backend is
  still live: a C3 panel accepts one connection at a time, and two schedulers
  polling the same 23 panels make each other's reads fail. Enable it with
  `docker compose up -d worker` only after the laptop backend is stopped.

Compose runs three services: `db` (PostgreSQL 16), `backend` (API,
`SCHEDULER_ENABLED=false`) and `worker` (`SCHEDULER_ENABLED=true`). Keeping the
scheduler in its own process is deliberate — otherwise every extra uvicorn
worker starts a duplicate copy of the polling jobs.

## Upgrades

```bash
# rebuild the bundle on Windows, scp it over, then:
sudo tar -xzf /tmp/zkteco-backend-<new>.tar.gz -C /tmp
sudo systemctl stop zkteco-worker zkteco-api
sudo rsync -a --delete /tmp/zkteco-backend/app/       /opt/zkteco-backend/app/
sudo rsync -a --delete /tmp/zkteco-backend/migrations/ /opt/zkteco-backend/migrations/
sudo rsync -a --delete /tmp/zkteco-backend/scripts/   /opt/zkteco-backend/scripts/
cd /opt/zkteco-backend
sudo -u zkteco .venv/bin/pip install -q -r requirements.txt
sudo -u zkteco bash -c 'set -a; . ./.env; set +a; .venv/bin/python -m alembic upgrade head'
sudo systemctl start zkteco-api zkteco-worker
```

## Rollback

```bash
sudo systemctl stop zkteco-api zkteco-worker
sudo -u zkteco bash -c 'set -a; . ./.env; set +a; .venv/bin/python -m alembic downgrade -1'
# restore the previous code bundle, then start again
```

## Operations

```bash
systemctl status zkteco-api zkteco-worker
journalctl -u zkteco-api -f
journalctl -u zkteco-worker -f
curl -s localhost:8000/health

# force a fleet refresh / log pull right now
curl -X POST localhost:8000/api/devices/sync/refresh
curl -X POST localhost:8000/api/logs/pull
```

Job outcomes are stored per device in the `sync_runs` table, which is the first
place to look when a panel silently stops reporting.

## Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| `/health` OK but all devices `offline` | no route/firewall to `10.100.1.x` | `scripts/check_panels.py` |
| Device `offline`, `last_error` mentions timeout | firewall blocking 4370, or panel down | `Test-NetConnection <ip> -Port 4370`, ICMP ping |
| `Connection refused` on 4370 | host reachable, panel not listening | confirm panel IP in its own config |
| Password error on connect | panel has a connection password | set `password` on the device record |
| `invalid literal for int()` | library/firmware parsing bug | already patched in `app/services/c3_compat.py` — if it reappears, a new firmware variant appeared |
| `Wrong table returned by panel` on logs | panel refuses the `transaction` table | documented in `docs/DEVICE_PROTOCOL_NOTES.md` §4 |
| Two sets of duplicated logs | two schedulers running | ensure `SCHEDULER_ENABLED=false` in `zkteco-api.service` |
| Dashboard empty after import | wrong DB (env var) | `curl localhost:8000/api/dashboard/summary` |

## Status (2026-09-18)

The Docker path above **was executed** on `192.168.0.27`. Only two things ever
needed a human: the one-time `sudo usermod -aG docker sentral`, and the SSH
password for the first key install. Everything else runs from here.

Verified on that host before and after the deploy:

- `10.100.1.3`, `.12`, `.21` — TCP 4370 reachable from the server (panels), and
  the Windows push agent at `10.100.1.100:8081` answers `/health` **200** with
  the token the backend holds.
- The API answers `/health` on `http://192.168.0.27:8000/`.

Still **not** done, deliberately: `worker` is not running (see above) and no
panel has ever been written to from that server — a first write should follow the
usual gate: `dry_run` → report → user's permission → one test panel → verify →
then widen.
