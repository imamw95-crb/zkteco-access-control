# ZKTeco C3 Access Control Backend

> **Working on this code with an AI agent (or onboarding a new developer)?**
> Read [`../AGENTS.md`](../AGENTS.md) first — it is the contract: what lives where,
> which rules must not be broken, the verified hardware facts, and the claims in older
> docs that are no longer true. This README stays the product overview.
>
> Machine-readable short version: [`.github/copilot-instructions.md`](.github/copilot-instructions.md).
>
> **Installing it?** [`docs/INSTALL.md`](docs/INSTALL.md) (dev lokal, produksi
> Linux, Docker, agent Windows, migrasi ZKAccess). **Using it?**
> [`docs/TUTORIAL.md`](docs/TUTORIAL.md) (dashboard per tab + resep harian).

Custom backend that replaces **ZKAccess 3.5** for ZKTeco C3-100/200/300/400
panels. It talks to the panels directly over the native Pull SDK (TCP 4370) and
has **no door or user limits**.

- **Backend**: FastAPI + SQLAlchemy 2.0 + Alembic
- **Database**: PostgreSQL (SQLite supported for local dev/tests)
- **Scheduler**: APScheduler (separate worker process)
- **Device protocol**: [`zkaccess-c3`](https://pypi.org/project/zkaccess-c3/)

> **Read this before planning the rollout**: the `zkaccess-c3` library implements
> only the *read* half of the Pull SDK — its command list jumps from
> `DATATABLE_CFG = 0x06` to `GETDATA = 0x08`, so **`0x07` (SETDATA) is missing**.
> That is a gap in the library, not in the panel: the official ZKTeco Pull SDK
> does support writing. Writing is therefore delegated to a small Windows agent
> that holds the official 32-bit `plcommpro.dll` — see [`agent/README.md`](agent/README.md).
>
> **The write path is proven against real hardware.** A test user was written to
> the `RUANGAN SERVER` panel and removed again, with the panel's full user list
> verified identical before and after
> ([`scripts/check_push_write.py`](scripts/check_push_write.py)). So
> `POST /api/devices/{id}/personnel/sync` works once `PUSH_AGENT_URL` is set, and
> answers **503** with an explanation when it is not.

## Quick start

### Docker (PostgreSQL + API + worker)

```bash
cp .env.example .env
docker compose up --build
```

- Dashboard: <http://localhost:8000/>
- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>

### Local development (no Postgres needed)

```bash
python -m venv .venv
.venv/Scripts/activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL=sqlite:///./zkteco.db
export SCHEDULER_ENABLED=false

alembic upgrade head
uvicorn app.main:app --reload
```

On Windows there is a one-click launcher at the workspace root:
`jalankan-backend.bat` starts the server **in the background** — it survives
closing the terminal or VS Code, is restarted automatically if it dies, and
auto-reloads `*.py` **and** `app/static/dashboard.html`. `status-backend.bat`
reports PID/port/log tail and `hentikan-backend.bat` stops it; inside VS Code
**Ctrl+Shift+B** runs the same thing. Details in
[`docs/INSTALL.md`](docs/INSTALL.md) §A.3.

### Load your panels

```bash
python -m scripts.import_devices_csv ../devices_input.csv
python -m scripts.live_check --limit 5      # smoke-test real panels
```

## Tests

```bash
python -m pytest
```

273 tests, no hardware required — the device layer is replaced by
`tests/fakes.FakeDeviceClient` through the `set_client_factory()` seam, and the
Windows agent is exercised over a real HTTP socket with the DLL swapped for a
recorder.

## Architecture

```
app/
├── api/                 REST routers (devices, personnel, logs, control, scan,
│                        dashboard)
│   └── deps.py          service wiring for FastAPI
├── services/
│   ├── c3_compat.py     workarounds for the library/firmware defects
│   ├── device_client.py the ONLY module that talks to zkaccess-c3
│   ├── device_service.py     device CRUD, info, health, discovery
│   ├── personnel_service.py  central personnel + device read-back
│   ├── panel_push.py         derives the records a panel needs (no I/O)
│   ├── push_agent.py         HTTP client for the Windows agent below
│   ├── network_scan.py       IP-range panel search (TCP probe + identity read)
│   ├── log_service.py        idempotent log ingestion, realtime events
│   └── sync_service.py       parallel, per-device isolated fleet jobs
├── workers/             APScheduler jobs + standalone worker process
├── models.py            Device, Personnel, AccessGroup, AccessLog, DoorSchedule, SyncRun
├── schemas.py           Pydantic request/response models (feed Swagger)
├── database.py          engine/session
├── config.py            settings (env / .env)
└── main.py              FastAPI app
agent/                   Windows push agent (official 32-bit Pull SDK DLL)
migrations/              Alembic
tests/                   pytest + device doubles
scripts/                 CSV import, live smoke test, boot check, panel table dump,
                         IP-range scan (scan_range.py), write-path check (the only
                         one that writes to a panel)
```

Key rules:

1. **Routes never touch `zkaccess-c3`.** They call a service; the service calls a
   `DeviceClient`. This is what makes the whole thing testable without panels.
2. **One failing device never blocks the fleet.** `SyncService` runs each device
   in its own thread *and* its own DB session, recording the outcome in
   `sync_runs`.
3. **Log ingestion is idempotent.** Every row carries a `dedupe_key` hash, so
   re-polling a panel never duplicates history.
4. **Reading and writing use different transports.** Reading happens in-process
   over `zkaccess-c3`. Writing happens over HTTP in a separate Windows agent,
   because the official Pull SDK DLL is 32-bit and cannot be loaded here. See
   [`agent/README.md`](agent/README.md).

## Feature status

| ZKAccess toolbar feature | Endpoint | Status |
|---|---|---|
| **Login + roles** (`admin` / `hr`) | `/api/auth/login\|logout\|me\|password` | ✅ **web UI**; sessions are database rows, so logout and deactivating an account really end them |
| **User accounts** (admin only) | `/api/users` | ✅ API + **web UI** (tab Pengguna) |
| Add / Edit / Delete device | `POST/PATCH/DELETE /api/devices` | ✅ API + **web UI** |
| Create access level | `POST /api/access-groups` | ✅ API + **web UI** |
| Get Information of Device | `POST /api/devices/{id}/info` | ✅ |
| Search / discover device | `GET /api/devices/discover` | ✅ |
| Search devices on an IP range | `POST /api/scan` (+`?stream=true`) | ✅ API + **web UI** |
| Online / offline health | `POST /api/devices/{id}/health` | ✅ |
| Personnel CRUD (central) | `/api/personnel` | ✅ |
| **Access levels** (CRUD, doors, members) | `/api/access-groups` | ✅ |
| **Departments** (master, hierarchy) | `/api/departments` | ✅ incl. tree view |
| **Access time zones** ("jam akses") | `/api/time-zones` | ✅ incl. 24-hour preset |
| Add door to access level | `POST /api/access-groups/{id}/doors` | ✅ API + **web UI** |
| Remove door from access level | `DELETE /api/access-groups/{id}/doors/{link_id}` | ✅ API + **web UI** |
| **Personnel count per device** | `GET /api/devices/{id}/personnel/count` | ✅ real count from the panel |
| **Get Personnel Data From Device** | `GET /api/devices/{id}/personnel` | ✅ |
| Import device users into central DB | `POST /api/devices/{id}/personnel/import` | ✅ |
| **Sync All Data to Device** | `POST /api/devices/{id}/personnel/sync` | ✅ via the [Windows push agent](agent/README.md); `?dry_run=true` lists the records without sending |
| Get Logs (transaction table) | `POST /api/logs/devices/{id}/pull` | This firmware **refuses a multi-field read** of `transaction` (SDK `-2`, library `Wrong table returned by panel`) — even two fields. So the table is read **one field per request** and the columns are stitched back by row position; if the row counts disagree the pull is **refused rather than merged**, because a Pin from one read next to a Cardno from another is a wrong access log. Through the agent this needs `ZK_AGENT_BUFFER_SIZE` raised (e.g. `4194304`) plus an agent restart. Errors distinguish the causes: `-112` = buffer, `-2`/`-107` = busy/timeout (retry). |
| Realtime monitoring | `GET /api/logs/devices/{id}/realtime` | ✅ (works, empty when idle) |
| Open / close door | `POST /api/control/devices/{id}/open` | ✅ |
| Cancel alarm / restart / normal-open | `/api/control/devices/{id}/…` | ✅ |
| Door schedules | `/api/door-schedules` | ✅ stored centrally; **not** pushed to panels |
| Dashboard table | `GET /api/dashboard/devices` | ✅ |

### Personnel count is a real count

`~MaxUserCount` (300) is only the capacity of the panel. The actual number of
users is obtained by reading the `UID` field of the `user` table — verified
against live panels:

| Panel | IP | Serial | Users on device |
|---|---|---|---|
| IBS PENERIMAAN | 10.100.1.3 | AJYS082162433 | 525 |
| ICU | 10.100.1.11 | AJYS082162438 | 387 |
| Depan Ranap 1 | 10.100.1.24 | REZ5092532010 | 522 |
| GIZI BELAKANG2 | 10.100.1.26 | ZMD9122563043 | 522 |
| FARAMASI LOG | 10.100.1.21 | ACC3062531073 | 27 |
| FARMASI RAJAL Lt1 | 10.100.1.18 | ACYT032350135 | 74 |

## Reliability

- Bounded retries with exponential backoff, and a socket reset between attempts
  (`DeviceService`/`ZKTecoDeviceClient._call`).
- Protocol errors (`ValueError`) are **not** retried — only transport failures.
- Every device job is recorded in `sync_runs` with its error message.
- Device failures are stored on the row (`is_online`, `last_error`) so the
  dashboard shows why a panel is down.
- `DEVICE_PARALLELISM` (default 8) caps concurrency; 23 panels poll fine.

## Configuration

See `.env.example`. Notable settings:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | postgres in compose | SQLAlchemy URL |
| `DEVICE_MAX_RETRIES` | 3 | retries per device call |
| `DEVICE_RETRY_BACKOFF` | 0.5 | base seconds for exponential backoff |
| `DEVICE_PARALLELISM` | 8 | fleet job thread pool size |
| `SCHEDULER_ENABLED` | false (API) / true (worker) | run polling jobs |
| `LOG_POLL_INTERVAL_SECONDS` | 60 | log polling cadence |
| `HEALTH_CHECK_INTERVAL_SECONDS` | 120 | health check cadence |
| `SCAN_ALLOWED_NETWORKS` | `10.100.1.0/24,192.168.1.0/24` | subnets an IP-range scan may touch |
| `SCAN_MAX_HOSTS` | 4096 | cap on addresses per scan |
| `SCAN_WORKERS` | 64 | parallel TCP probes |
| `PANEL_NETWORK_WRITE_ENABLED` | false | allow writing a panel's **own** IP address |
| `AUTH_SEED_USERS` | `admin:admin:admin,hr:hr:hr` | first accounts (`user:password:role`), created **only** when the `users` table is empty |
| `AUTH_SESSION_HOURS` | 12 | session lifetime |
| `AUTH_COOKIE_SECURE` | false | set true when the dashboard is served over HTTPS (a `secure` cookie is never sent over plain HTTP) |
| `AUTH_COOKIE_NAME` | `zk_session` | cookie (and `Authorization: Bearer` token) name |

## Migrating from ZKAccess 3.5

The legacy data lives in the ZKAccess SQL Server database. It is read
**read-only** — `ZKAccessSource` refuses any statement that is not a
`SELECT`/`WITH` — and written into this backend.

Credentials come from the environment, never from code:

```bash
export ZKACCESS_SERVER='10.100.1.100\SQLEXPRESS'
export ZKACCESS_USER=sa
export ZKACCESS_PASSWORD=...

pip install pyodbc            # needs an ODBC Driver for SQL Server

python -m scripts.check_zkaccess_source        # connectivity + read-only guard
python -m scripts.migrate_from_zkaccess --dry-run
python -m scripts.migrate_from_zkaccess        # idempotent
python -m scripts.verify_migration             # set-by-set reconciliation
```

What is migrated (access logs are deliberately **not**):

| ZKAccess | rows | becomes |
|---|---|---|
| `Machines` | 23 | `Device` (matched on IP+port) |
| `USERINFO` | 528 | `Personnel` (`Badgenumber` → `employee_id` + `pin`) |
| `DEPARTMENTS` | 28 | `Personnel.department` |
| `acc_levelset` | 12 | `AccessGroup` |
| `acc_levelset_door_group` | 87 | `access_group_doors` |
| `acc_levelset_emp` | 2,037 | `personnel_access_groups` |
| `personnel_issuecard` / `USERINFO.CardNo` | 524 | `Personnel.card_number` |

### Two schema additions this required

The legacy data does not fit a "one group per person, one door per group" model:

- **`personnel_access_groups`** — 527 people hold **2,037** memberships (3.87
  each, up to **11**). The single `Personnel.access_group_id` column is kept for
  back-compat only and must not be used for access decisions.
- **`access_group_doors`** — `AccessGroup.door_numbers` is a comma-separated
  list and cannot express a group spanning multiple panels, e.g. `TEKNISI/IT`
  covers 22 doors across 20 devices.

`PersonnelRead.access_group_names` exposes the memberships through the API.

### Assigning a person to an access level

Two ways, both end up in `personnel_access_groups`:

```bash
# a) in one call, when creating the person
curl -X POST localhost:8000/api/personnel -H 'Content-Type: application/json' \
  -d '{"employee_id":"1001","name":"Budi","access_group_id":7}'

# b) afterwards, from the access-level side (bulk friendly)
curl -X POST localhost:8000/api/access-groups/7/members -H 'Content-Type: application/json' \
  -d '{"employee_ids":["1001","1002","1003"]}'
```

| Field | Meaning |
|---|---|
| `access_group_id` | **Additive.** Makes sure the person holds this level; other levels are untouched. |
| `access_group_ids` | **Replaces** the whole set. Use this to *move* someone between levels, or send `[]` to revoke all access. |

The additive behaviour of the singular field is deliberate: a person can
legitimately hold up to ~11 levels, so a single-value field must not silently
delete the others.

### Verified figures

`scripts.verify_migration` passes on every category:

```
Devices            23 = 23
Personel          528 = 528          kartu tidak cocok: 0
Grup akses         12 = 12
Grup -> pintu      87 = 87
Keanggotaan      2037 = 2037
orang dengan >1 grup 506 · grup terbanyak untuk 1 orang 11
```

Independent cross-check: `Machines.usercount` in ZKAccess matches the counts
read straight from the panels over the Pull SDK (525 / 387 / 523 / 27 / 522),
so both the source data and this backend's reader are confirmed correct.

## Access time zones ("jam akses")

A time zone is the weekly schedule an access level runs on. On the panels this
is the `timezone` table; in ZKAccess it is `acc_timeseg`. A zone holds up to
three segments per weekday, which is what the firmware supports.

**The 24-hour zone is created automatically.** Every panel here runs on time
zone slot 1, so the definition (`24 Jam`, every day `00:00-23:59`) is seeded on
startup and by the Alembic data migration. All 12 migrated access levels point
at it, so they show `24 Jam` instead of a bare number.

```bash
# list / inspect
curl localhost:8000/api/time-zones
curl localhost:8000/api/time-zones/1        # includes a per-day `week` view

# make sure the 24-hour preset exists (idempotent)
curl -X POST localhost:8000/api/time-zones/presets/24-hours

# a working-hours zone for Mon..Fri
curl -X POST localhost:8000/api/time-zones -H 'Content-Type: application/json' -d '{
  "name": "Jam Kerja", "device_timezone_id": 2,
  "slots": [
    {"day_of_week":1,"slot":1,"start_time":"07:00","end_time":"17:00"},
    {"day_of_week":2,"slot":1,"start_time":"07:00","end_time":"17:00"}
  ]}'

# point an access level at it
curl -X PUT localhost:8000/api/access-groups/7/time-zone \
  -H 'Content-Type: application/json' -d '{"time_zone_id": 2}'
```

Details worth knowing:

- `day_of_week` follows the **device** order: **0 = Sunday** .. 6 = Saturday,
  matching the panel's `SunTime1`..`SatTime1`. (This differs from
  `DoorSchedule`, which is Monday-based.)
- A segment whose start equals its end is unused — the firmware represents an
  empty slot as `00:00`-`00:00`.
- The panel stores each end as `HHMM`; `2359` means 23:59. `TimeZoneSlot`
  exposes `from_device_value`/`to_device_value` for that translation.
- Deleting a zone that access levels still use is refused with `409` rather
  than silently orphaning them.

`AccessGroup.device_timezone_id` is the slot number the panel uses, so a group
needs no second foreign key — the lookup is by that number.

## Departments

Departments are a real master table (`departments`), not a free-text field, so
the hierarchy survives and renaming one updates every person at once.

```bash
curl localhost:8000/api/departments            # flat list + personnel counts
curl localhost:8000/api/departments/tree       # nested hierarchy
curl localhost:8000/api/departments/9          # detail: full_path, parent, children

# personnel can reference a department by id or by name
curl -X POST localhost:8000/api/personnel -H 'Content-Type: application/json' \
  -d '{"employee_id":"1001","name":"Budi","department_id":9}'
curl -X POST localhost:8000/api/personnel -H 'Content-Type: application/json' \
  -d '{"employee_id":"1002","name":"Ani","department":"ICU"}'   # created on first use
```

Behaviour worth knowing:

- `department` (name) is resolved against the master and **created if unknown**, so
  the ZKAccess importer and simple clients keep working. An unknown
  `department_id` returns `404`.
- `PUT/PATCH` accepts `clear_parent: true` to move a department to the top level,
  and `PATCH /api/personnel/{id}` accepts `clear_department: true`.
- Cycles are rejected (`409`): a department cannot become its own ancestor.
- Deleting a department still in use is refused (`409`); deleting one that has
  children re-parents them to its own parent.
- `Personnel.department` is a **property** reading `department_ref.name` — there is
  only one place the data lives, while the API response shape is unchanged.

The migration promoted the old free-text column into this table: distinct names
became rows, every person was linked, and only then was the text column dropped.
Upgrade **and** downgrade were both verified to preserve the data.

## Login and roles

Every `/api` route needs a session. `/api/auth/login` and `/api/auth/logout` are
the only exceptions — logging out has to work precisely when the session is
already broken. Two roles, and the difference is **which part of the dashboard** a
person may use, not how much of the fleet they see:

| Role | May open | Sessions |
|---|---|---|
| `admin` | everything: Device, door control, Cari Device, a panel's own address, jam akses, **Pengguna** (accounts) | |
| `hr` | people data only: **Personnel** (including the push to panels), **Departments**, **Access Levels**, plus the read-only **Monitoring** and the device list (a door picker needs it) | |

```bash
# log in (a script has no cookie jar, so the session also works as a bearer token)
curl -s -c jar -X POST localhost:8000/api/auth/login \
     -H 'Content-Type: application/json' -d '{"username":"admin","password":"admin"}'
curl -s -b jar localhost:8000/api/devices            # 200
curl -s -b jar localhost:8000/api/users              # 200 for admin, 403 for hr
curl -s -X POST localhost:8000/api/auth/logout -b jar
```

- **The first accounts** are created at startup, only when the `users` table is
  completely empty: `AUTH_SEED_USERS` (default `admin:admin:admin,hr:hr:hr`, i.e.
  `username:password:role`). They are flagged `must_change_password`, so the
  dashboard keeps warning until the password is changed — a login is never blocked,
  because locking the only admin out of a LAN dashboard is worse than a default
  password. Seeding never touches an existing account: restarting the backend
  cannot reset a password somebody changed.
- **Passwords** are PBKDF2-HMAC-SHA256 (standard library, no new dependency) with
  a per-password salt; a minimum of 4 characters applies to whatever is set from
  the API, which the seeded defaults are exempt from (they come from the operator's
  own environment).
- **Sessions** are rows in `auth_sessions`, not signed cookies, so logout — or
  deactivating an account — really ends the session on the server. Only the
  SHA-256 of the cookie value is stored, the cookie is `HttpOnly` + `SameSite=Lax`
  (set `AUTH_COOKIE_SECURE=true` when serving over HTTPS), and it lasts
  `AUTH_SESSION_HOURS` (default 12).
- **Guards live on the server** (`app/api/deps.py`, applied in `app/api/__init__.py`).
  Hiding a tab in the dashboard is convenience, never the lock. The last active
  admin cannot be demoted, switched off or deleted, and you cannot delete the
  account you are logged in with.
- `tests/test_auth.py` walks **every** registered `/api` route and fails if one
  answers anything but **401** without a session, and if any admin-only route
  answers anything but **403** for an `hr` session — so a new endpoint cannot
  quietly ship without its guard.

## Web UI

`http://localhost:8000/` serves a small dashboard (no build step, one static
HTML file) with up to seven tabs, behind a login. It opens on **Personel** —
looking somebody up (or fixing a card / a level) is the daily job, while the
Device tab is for changing hardware — and one `selectTab()` switches both the
highlight and the visible panel, so the boot path cannot drift from a click.

The shell itself is public (it holds no data; every number and name on it comes
from `/api`), and the page asks `/api/auth/me` before it loads anything, so an
unauthenticated visitor never sees a tab, a count or a name. `ROLE_TABS` is the
only place the UI decides what to show: an `hr` session gets Personnel, Access
Level, Departments and Monitoring, and the Device / Cari Device / Pengguna tabs
stay hidden — pressing them by hand would only answer 403.

| Tab | What it does |
|---|---|
| **Device** | The ZKAccess-style toolbar table: name, serial, IP, personnel count, firmware, online status. Includes the **add-device form** (name, IP, port, password, location/area) which doubles as the edit form, a per-row **Info / Edit / Hapus** column, "Cari device di jaringan" (UDP broadcast discovery, click a result to prefill the form), "Get Info (semua device)" to refresh the whole fleet, a **Buka pintu (remote)** card, and a **Kirim data personel ke panel** card. The per-row **Info** button also shows the panel's own network configuration beside the address this database dials. |
| **Access Level** | All access levels with door/member counts and their **jam akses**, plus a form to **create a new level** (name, description, time zone). Click one to see its doors, its members, change its time zone, **add/remove doors (device + pintu 1-4)**, and **add many members at once** (one badge per line, or comma separated). Adding either of those only changes the dashboard, so the card says what has not reached the hardware yet and offers the write (see below). |
| **Personel** | The personnel list with their access-level pills, a search box, and a form that both **adds** and **edits** a person (badge, name, card, department and any number of access levels). Saving also sends that person to every panel their access levels reach (see below), and **Samakan ke semua panel** repeats it on demand. |
| **Departemen** | The department master with its parent and personnel count, plus a search box. |
| **Cari Device** | IP-range search for panels, one CIDR per line (`10.100.1.0/24`, `192.168.1.0/24`). The broadcast button on the Device tab only reaches the backend's own subnet, so a range scan is what finds a panel on a routed subnet; it sweeps TCP 4370 in parallel, then reads each panel's serial/firmware one at a time (a C3 panel accepts only one connection). Results are matched against the device table **by serial first**, so a panel whose address changed is still recognised instead of looking like a new device. The table also shows the address the **panel itself** reports (read-only) next to the one it answered on. Progress is streamed per batch. Each row offers **Tambah** (registers the device, carrying the Lokasi/Area fields) or, for a registered panel answering at another address, **Perbaiki IP di dashboard** — that corrects the stored address only. No panel configuration is written: a wrong address written into a panel makes it unreachable and it can only be fixed on site. |
| **Monitoring** | Live view of the fleet: a panel-health grid (online/offline, last seen, last log pulled, users read from the panel, last error) and an access-event feed (time, panel, door, person, card, verification mode, accepted/denied) that refreshes itself. The feed is deliberately read from the **database**, never from the panels — a C3 panel accepts one connection at a time, so polling panels from the page would fight the scheduler and the push agent and could make healthy panels look offline. It holds one NDJSON stream (`GET /api/monitor/stream`) and stops it when you leave the tab. Because the feed only moves when something polls the panels, the page says so in red when `SCHEDULER_ENABLED=false` (the dev launcher's and `zkteco-api`'s default) and offers **Tarik log sekarang** (`POST /api/logs/pull`), which walks all 23 panels one at a time, read-only. Only `EventType 0` (accepted) and `27` (unknown card) are classified; every other code is shown uncoloured under its own name, because guessing "accepted" on a denial is worse than no colour. Filter by name/badge/card/panel, or tick **Hanya akses ditolak** to see denials alone. A **Tanggal** control (defaults to today) drives both the feed and the pull: `since`/`until` limit what is **stored**, never what is read — a panel always hands over its whole transaction buffer, so rows outside the day are read, reported as `skipped`, and dropped. `_card_or_none()` keeps a row read by either path (library or agent) hashing to the same `dedupe_key`. |
| **Pengguna** (admin only) | Accounts: name, role, status, last login, and per-row **Jadikan Admin/HR**, **Nonaktifkan/Aktifkan**, **Reset password**, **Hapus**. Deactivating or resetting cuts that account's live sessions immediately; the last active admin cannot be demoted, switched off or deleted, and the account you are logged in with cannot be deleted. Deleting an account says explicitly that personnel, departments and access levels are unaffected — otherwise the button reads like "remove the person". |

The door-control endpoints (`/api/control/devices/{id}/open`) **are** exposed in
the UI, as the "Buka pintu (remote)" card on the Device tab. Opening a door moves
a physical lock, so the card is deliberately placed at the bottom of the tab,
away from the routine device buttons, and every click goes through a
confirmation that names the panel, the door and the duration. Default duration is
**15 seconds** — long enough to walk through, short enough not to be left
standing open. A cancelled confirmation says so explicitly rather than leaving a
stale "opened" message behind. Nothing is sent unless the confirmation is
accepted.

The push endpoint (`POST /api/devices/{id}/personnel/sync`) is exposed as the
**Kirim data personel ke panel** card on the same tab. Writing personnel data
decides who may open which door, so the payload size is always reported back
(`42 user, 42 authorize`) and the real write is confirmed first. The dry run is a
separate button rather than a checkbox, because it is the safe way to see how
large a change would be before committing it to a live panel; it reports
`Dry run - belum ada yang ditulis.` so a dry run is never mistaken for a write.

### Saving a person also refreshes the panels

A panel keeps whichever rights it was last **written** with. Changing somebody's
access level in the dashboard therefore does nothing to the hardware until the
person is pushed, and the difference is invisible from the browser — the level
says `ICCU` while the door still opens for the level that was removed. So the
personnel form carries **Setelah simpan, kirim ke semua panel**, ticked by
default: pressing *Simpan perubahan* commits the edit and then runs
`POST /api/personnel/sync?personnel_ids=<id>` for that one person, which writes
where they now belong and **revokes** the rights they no longer hold. Only this
person is sent; the hundreds of other users already on each panel are untouched.

The tick can be cleared, and that is deliberate rather than a leftover: the write
reaches the hardware, and there is one case where it must not go out yet — a card
number that has not been confirmed at a reader. `SetDeviceData` stores whatever
it is given, so a wrong number is written faithfully to every panel at once and
the door simply never opens, with no error anywhere. Save with the tick cleared,
test the card on **one** panel, and push once it is known to work.

Because the sweep walks all 23 panels one after another, it reports itself panel by
panel in a popup (`POST /api/personnel/sync?stream=true`, newline-delimited JSON):

* each panel appears the moment it answers — green `✔ NAME - SELESAI, 3 hak ditulis`
  (or `sudah sesuai` when nothing had to change), red `✖ NAME - error`, or muted
  `- NAME - tidak memuat orang ini` for the panels that only had to be checked;
* a counter shows how many panels have been checked so far, and the popup cannot be
  dismissed until the sweep ends (the panels accept only one connection at a time,
  so a second click would just make the next panel answer "busy");
* the last line is the verdict: `SELESAI` with what was written and revoked, or the
  panels that failed plus, in so many words, that those panels **still hold the old
  data** and the action has to be repeated. The same text stays in the form's
  message area afterwards, because the popup is transient and that is the record.

Streaming is opt-in: `POST /api/personnel/sync` without `stream` still returns the
whole sweep as a single JSON body, which is what the operational scripts use.

### A level change only reaches the panels when it is written

Adding a door (device + pintu) or members to an access level changes the dashboard,
not the hardware: every panel keeps the rights it was last written with. So both
actions say what is still missing and offer the write.

* **A new door** needs the level's members on *that one* panel: `pushPanel()` writes
the panel the door was added to.
* **New members** are written to **that level's panels only** — the request carries
`device_ids` for the level's doors, so the popup lists those panels instead of the
twenty others that have nothing to do with this level. (`Samakan ke semua panel` on
the Personel tab remains the wide sweep, because that one exists to hunt stale
rights everywhere.)
* **Removing a member** is the dangerous direction, and it is the one case that must
stay wide: leaving the level changes nothing on the hardware, so the panel would keep
opening the door for somebody who no longer belongs there. The card therefore offers a
sweep over **every** panel — the stale grant can be on any of them — and that sweep is
what revokes it.

Declining, or a panel that failed, leaves an explicit `PANEL BELUM DIUBAH` in the
card. The write can always be run again by hand: **Kirim data personel ke panel**
on the Device tab rewrites one panel, and **Samakan ke semua panel** on the
Personel tab rewrites one person everywhere.

The other direction is covered by the save itself (previous section): changing
somebody's access level and saving sends that person to every panel of the level,
including panels added since they were last pushed.

### Confirmations are rendered in the page, not by the browser

Every destructive or physical action (open door, delete device, remove a door or
member from a level, switch the person being edited) asks through `askConfirm()`,
which draws a real element in the page. It deliberately does **not** use the
browser's native `confirm()`.

A native dialog is not dependable here. Once a browser decides to stop showing
them — Chrome offers *"prevent this page from creating additional dialogs"* after
a few, and embedded webviews can block modals outright — `confirm()` returns
`false` immediately, with no dialog and no explanation. The operator clicks a
button and sees nothing but "Dibatalkan", with no way to tell a refusal apart
from a broken feature. Rendering the confirmation in the page always appears, and
names exactly what is about to happen.

The device is reached over the network, so a failed open is normal rather than
exceptional: `POST /api/control/.../open` returns HTTP 200 with
`{"success": false, "error": "..."}`, and the UI reports that error text.

> **This endpoint has no authentication.** Anyone who can reach the port can open
> any door on any panel. See the open issue below — it matters far more now that
> there is a button for it.

## Changing a device's IP

Two different addresses are involved, and only one of them can be changed from here.

**The address this database dials** — Device tab → *Edit* → *IP address*. This is what
every read, push and door command uses, so a moved or re-addressed panel has to be
kept in step with it here. It is an ordinary `PATCH /api/devices/{id}`, guarded
against two devices claiming the same IP and port. The **Cari Device** tab offers the
same fix in one click when a scan finds a registered panel answering at another
address ("Perbaiki IP di dashboard", with a confirmation naming both addresses) — a
scan is the only feature that can see that drift when the panel moved to a subnet the
broadcast search cannot reach.

**The address configured on the panel itself** — read **and** written, but writing is
opt-in. *Info* on a device row, and the **IP di panel** column of a scan result, show
the panel's own `IPAddress`, `NetMask`, `GATEWAY` and `MAC` beside the values this
database holds, and both call out a difference:

| Parameter | Nilai di panel | Dipakai aplikasi |
| --- | --- | --- |
| IP address | `10.100.1.12` | `10.100.1.12` |
| NetMask | `255.255.255.0` | - |
| MAC | `00:17:61:FF:41:98` | `00:17:61:FF:41:98` |

The card **Ubah alamat jaringan panel** (Cari Device tab) is the equivalent of
ZKAccess' *Modify IP Address*: `POST /api/devices/{id}/network` writes
`IPAddress`/`NetMask`/`GATEWAY` into the panel through the agent's `SetDeviceParam`.
It is **switched off by default** — `PANEL_NETWORK_WRITE_ENABLED=true` is required, and
until then a write request is refused with **503** and a message naming the switch and
the safe alternative ("Perbaiki IP di dashboard", database only). That default is the
safety mechanism, not a placeholder: this is the only operation in the system that can
leave a panel unreachable **with no way back**.

When it is switched on, the guards are:

- `dry_run` **defaults to true** on the endpoint, and the UI keeps the write button
  disabled until a dry run of the *exact same* values has been shown to the operator
  (change one digit and it locks again).
- The new address must fall inside `SCAN_ALLOWED_NETWORKS` (a panel sent beyond that
  could never be dialled again) and must not be used by another device on that port.
- The panel is read first, and its serial must match the device record — otherwise we
  are about to re-address the wrong panel.
- A mandatory confirmation names the old and the new address, and says the panel cannot
  be fixed from here if it goes wrong.
- After the write the connection to the old address is gone, so verification dials the
  **new** address (4 attempts — panels also refuse busy connections). The stored address
  is updated **only** if that succeeds; otherwise the record is left alone and the result
  says the panel is now unreachable and must be checked on site.
- The whole configuration is sent in one call (IP, netmask, gateway, and the MAC read
  from the panel). Whether `SetDeviceParam` clears parameters it was not given — as
  `SetDeviceData` does — is **not known**, so nothing is omitted.

Two measurements shape those guards:

- **`GATEWAY` is not reported by this firmware.** The `c3` library drops the key
  entirely and the agent's official-SDK read answers `""` (measured on `10.100.1.5`).
  `IPAddress`, `NetMask` and `MAC` read fine on both paths. So a write could not copy
  the gateway over from the panel — it would have to be supplied by the operator, and
  getting it wrong cuts the panel off from its default route.
- **The connection dies the moment the address changes**, so a write can only be
  verified by dialling the *new* address — and if that fails there is no remote way
  back.

Reading those four parameters is what makes the drift visible at all. It is also why
`mac_address` is populated now: the library's `.mac` property answers empty on this
firmware, while the `MAC` parameter does not.

## Card and badge uniqueness

A badge (`Personnel.employee_id`) and a card (`Personnel.card_number`) identify
**one** person. Two people sharing a card would both open the same doors, so
both columns carry a `UNIQUE` constraint *and* the service checks them before
writing. The service check exists because a raw `IntegrityError` only says
"UNIQUE constraint failed" — the API instead answers with the field, the value
and the current holder:

```json
{"detail": "No. kartu 5001 sudah dipakai Budi. Kartu tidak boleh dobel."}
```

Three details that matter:

- `card_number` is nullable, so an empty or whitespace-only card is stored as
  `NULL`. Without that normalisation `""` would count as a real value and the
  second person saved without a card would collide with the first.
- `employee_id` is trimmed, so `"  1001  "` is recognised as badge `1001`.
- Re-saving a person keeps their own card (the check excludes the row being
  updated), and setting the card to `null` releases it for re-issue.

## The number printed on a card is not always the number the panel reads

Symptom: the person is on the panel, the `user` row and the `userauthorize` row
read back exactly like a working colleague's, and yet tapping the card does
nothing.

Cause: many cards carry a **printed serial** that is not the **Wiegand value the
reader emits**. `SetDeviceData` writes back whatever number you hand it, so a
wrong number is stored faithfully and simply never matches a tap. Nothing on the
panel reports an error, and both rows look perfect.

How to prove it. Every panel keeps an access log in its `transaction` table. An
**unknown** card is recorded with `Pin=0` and `EventType=27`, next to the
`Cardno` the reader actually saw:

```bash
curl -H "Authorization: Bearer $ZK_PUSH_AGENT_TOKEN" \
  "http://127.0.0.1:8081/panels/10.100.1.12/tables/transaction?fields=Pin,Cardno,EventType"
```

```
Pin Cardno     EventType   <- card presented, not recognised
0   2150141426 27
0   2150141426 27
```

Then look for the number you wrote:

- **A row with that number and `Pin=0`** — the number is right and the
  *authorization* is wrong. Check `userauthorize` (time zone and door mask).
- **No row at all** — the panel has never read that number. This is the case
  that matters, because it is silent. Either the card was tapped at a different
  door, the card is not readable by that reader at all (wrong frequency), or the
  printed number is not the number the reader emits.

Measured 2026-09-16 for the card being enrolled at RUANGAN SERVER:

| number | occurrences in the transaction logs of all 23 panels |
| --- | --- |
| `4063788292` — what we wrote | **0** |
| `2150141426` — what the reader saw | **11**, all at RUANGAN SERVER, enrolled nowhere |

The fix is to put the **read** number on the person and push again. Keep the
printed number somewhere — `Name` is the honest place, because it is the only
label the card itself carries and it is what the person will quote back to you.

**A record only reaches a panel when it is pushed.** Creating somebody in the
dashboard writes nothing to any panel; the *Kirim data personel ke panel* card
(and `POST /api/devices/{id}/personnel/sync`) does. A test person that was never
pushed fails exactly like a wrong card number, and for the same invisible reason.

## Pushing: which panels, and what it overwrites

A panel's expected user list is the members of every access level that includes
one of its doors. Those levels span many panels — `TEKNISI/IT` alone covers **22
doors across 20 panels** — so a **per-device** push refreshes the one panel it is
aimed at and silently leaves the rest stale. That is how somebody gets granted a
level and ends up able to open one door out of twenty.

`POST /api/personnel/sync` fixes that: it makes those people's access match the
dashboard on *every* panel, and reports per panel.

```bash
# one person, everywhere they belong — and nowhere they do not
curl -X POST 'http://localhost:8000/api/personnel/sync?personnel_ids=531'

# look first — the dry run never touches the network
curl -X POST 'http://localhost:8000/api/personnel/sync?personnel_ids=531&dry_run=true'
```

Response:

```json
{ "personnel_ids": [531], "device_count": 23, "covered_count": 14,
  "succeeded": 23, "failed": 0,
  "written_authorize": 0, "deleted_authorize": 7,
  "results": [ { "device_id": 9, "device_name": "RUANGAN SERVER",
                 "covered": false, "ok": true,
                 "deleted": { "userauthorize": 1 } } ] }
```

Three properties make this the safe default:

- **Only the named people are touched.** Everybody else on those panels is left
  exactly as they are, and only the differences are written.
- **Access that should no longer exist is removed.** See below — writing alone was
  never enough.
- **One unreachable panel does not abort the rest.** The operator needs to see
  exactly which panel still needs attention.

### Taking access away — the half a push used to miss

Writing only ever *adds*. Take somebody out of an access level and the old
`userauthorize` row stayed on every panel of the level they left, so the door kept
opening. Measured: a person moved from `TEKNISI/IT` to `AKSES UMUM KARYAWAN` still
held `AuthorizeDoorId 1` on RUANGAN SERVER and could still open it.

The fix is that a named person's rights are **authoritative**: the desired set for
each panel replaces whatever is there, so rows that are no longer granted are
deleted. That check runs on **every** panel, including ones the person has left —
otherwise there would be nothing to delete on precisely the panels that matter.

Deliberately narrow, because the opposite mistake is worse:

- **A whole-panel push still only adds.** Panels hold people this database cannot
  account for — OK PETUGAS carries 405 users against the 361 we can explain — and
  revoking rights for pins we merely failed to derive would lock out strangers.
- **Only the named people are revoked.** Revocation follows the ids the caller
  passed, not everybody the sweep happens to read.
- **`user` rows are never deleted.** Access is granted by `userauthorize`, and
  deleting a user row and re-adding it would cost the fields this project does not
  send (see below). A person removed from every level keeps a harmless name on the
  panel with no way through any door.

Overlap is handled by comparing, not by assuming: a person moved between two levels
that both reach a panel keeps their access there and nothing is rewritten. In the
measured run above only 7 of the 14 panels they left actually lost a row — the rest
were still covered by the level they moved *to*.

`scripts/verify_person_on_panels.py` reads **every** panel back and reports the
three outcomes separately — matching, a right that should have gone, a right that
should be there — so the answer does not depend on trusting the push:

```bash
python -m scripts.verify_person_on_panels --badge 2150141426
```

### Only what changed is written

A panel holds every member of every level that reaches it — five hundred people on
this fleet. Sending all of them to change one used to mean rewriting hundreds of
records that were already correct, each one a chance to disturb data this project
does not manage.

So the panel is read first, and only the differences are written:

|  |  |
| --- | --- |
| considered | every person the panel should hold |
| **written** | those the panel is missing, or whose managed fields differ |
| unchanged | everything already correct, left completely alone |

Measured on RUANGAN SERVER (43 people, nothing changed since the last push):

```json
{ "written":   { "user": 0, "userauthorize": 0 },
  "unchanged": { "user": 43, "userauthorize": 43 } }
```

A push that cannot read the panel **writes nothing** and says so. Falling back to
"send everything" would do precisely what comparing exists to prevent.

### Fields a push does not send — and why they are carried across

The `user` record is built from `Pin`, `Name`, `CardNo` (plus `StartTime` /
`EndTime` when validity dates are set). The panel's other fields — `Password`,
`Group`, `SuperAuthorize`, `Disable` — are not ours to set.

**`SetDeviceData` replaces a record rather than patching it, so a field left out of
the request is cleared.** This is measured, not suspected. Rewriting one person on
RUANGAN SERVER to correct their name took the panel from `Password = 123456` to
empty. Live panels do hold data in those fields:

| panel | what was found |
| --- | --- |
| RUANGAN SERVER | `Fega` (Pin `101138154`) had `Password = 123456` |
| GIZI BELAKANG, AREA TEKNISI BELAKANG | 13–14 users carry `SuperAuthorize = "A.Md.Kep"` |

Every record that has to be rewritten therefore carries the panel's own values for
those fields across, so a rewrite is lossless. `plan.user.carried_over` names the
Pins where that mattered.

Verified on hardware by giving a test record a password, changing its name to force
a rewrite, and reading it back: the name updated and the password survived.

`personnel_ids` remains useful for scoping a push deliberately, but it is no longer
the only thing standing between a routine rename and somebody's keypad password.

### A failed read-back is not a failed write

The agent reads each table back so the caller can verify rather than trust. Panels
answer `-2` (busy) or `-112` (reply larger than the buffer) intermittently, and
that used to fail the whole request — reporting `Gagal: IGD 2` for a panel whose
user list did in fact contain the new Pin. The read-back now retries, and if it
still cannot read it reports `verify_error` alongside `"verified_records": null`
while keeping `written` intact. **"Could not verify" and "did not write" are
different answers**, and conflating them costs an operator a pointless
investigation.

Ordinary reads and connects are retried the same way (three attempts, half a second
apart), because the same transient errors hit them too. Measured: a sweep of 20
panels reported two read failures and both passed on an immediate re-run. The last
error is still raised once every attempt fails, so a panel that is genuinely down
reports as down rather than being retried into silence.

`scripts/verify_person_on_panels.py` reads the panels back and keeps the three
outcomes apart — present, missing, unreadable — because it exists to answer "did
the push reach everywhere?" without guessing:

```bash
python -m scripts.verify_person_on_panels --badge 2150141426
```

## Editing personnel

The Personnel tab's form is shared between add and edit. Clicking **Edit** on a
row preloads that person's badge, name, card, department and access levels, and
saves with `PATCH`. `PersonnelRead` exposes `access_group_ids` (not just names)
so the checkbox picker can be filled from ids; matching on names would break the
moment a level is renamed.

The levels are saved with `access_group_ids`, which **replaces** the set — the
picker shows the complete membership, so what is ticked is what is stored. An
emptied card field sends `card_number: null` (releasing the card), and an emptied
department sends `clear_department: true`. A `department_id: null` on its own
does *not* detach, so a form that always includes the key cannot wipe it.

**Switching the edit target is confirmed, not silent.** The form's target lives
in a variable rather than the DOM, so an accidental click on another row's Edit
button would otherwise send the next save to the wrong person's record. The
click is therefore refused unless confirmed. Related: the scroll that reveals
the form is deliberately instant — an animated scroll keeps moving after a click
begins, which can make the click land on a table row instead of a field.

## Deployment note

Run the API and the worker as **separate processes**. The compose file does
this: `backend` serves HTTP with `SCHEDULER_ENABLED=false`, `worker` runs
`python -m app.workers.run` with the scheduler enabled. Running both in one
process makes every uvicorn reload spawn a second scheduler.

For a full runbook (target server recon, pre-flight gate, systemd, nginx,
upgrades, rollback, troubleshooting) see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Pre-flight before deploying anywhere

The backend is useless on a host that cannot reach the panels:

```bash
python -m scripts.check_panels --csv devices_input.csv
```

Exit code 0 with `23/23` means the host is good. `0/23` means no route to
`10.100.1.0/24` — fix routing instead of deploying.

