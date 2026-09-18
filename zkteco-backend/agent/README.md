# ZKTeco Push Agent (Windows)

The backend is a 64-bit Linux service. Writing to a panel needs the official
ZKTeco **Pull SDK**, which ships as a **32-bit Windows DLL** (`plcommpro.dll`) —
it cannot be loaded by a 64-bit Linux process. This agent is the bridge: it runs
on Windows, holds the DLL, and exposes the device tables over HTTP so the backend
can push users and access rights.

```
[ Linux backend + Postgres + web UI ]        [ Windows push agent ]
        POST /panels/{ip}/tables/user  ─────►  plcommpro.dll ──► C3 panel
```

Why not just use `zkaccess-c3` (the pure-Python library the backend already uses)?
Because it implements only the **read** side of the Pull SDK. Its command list goes
`DATATABLE_CFG = 0x06` → `GETDATA = 0x08`: `0x07`, which is **SETDATA**, was never
implemented. That is a gap in the library, not in the panel.

---

## 1. Prerequisites

| | |
|---|---|
| **32-bit Python** | 3.8 or newer. **This is the one thing that cannot be skipped** — a 64-bit Python cannot load a 32-bit DLL. Get "Windows installer (32-bit)" from python.org. |
| **Official Pull SDK** | The SDK for C3/C4 access controllers. `plcommpro.dll` **plus every other `pl*.dll` beside it** — see the `-201` trap below. ZKTeco's Download Center → *SDK*; the site is JavaScript-driven and cannot be scripted, so this is a manual download. |
| **Network access** | The agent must reach the panels on TCP 4370 (same LAN/subnet). |
| **VC++ Redistributable x86** | [`vc_redist.x86.exe`](https://aka.ms/vs/17/release/vc_redist.x86.exe). Without it the DLL fails to load with a misleading *"Could not find module … or one of its dependencies"*. |

No `pip install` is required — this agent only uses the standard library.

> **Fastest path: the installer.**
> [`installer/ZkPushAgent-Setup.exe`](installer/README.md) bundles its own 32-bit
> Python, copies your `pl*.dll` into one folder (so `-201` cannot happen), installs
> the VC++ x86 runtime when missing, sets the environment variables, registers the
> scheduled task as `SYSTEM`, then starts the agent and *proves* it with
> `GET /health` — writing a report to `CHECK-REPORT.txt`. Everything below is what
> that installer does for you, by hand.

### Install the 32-bit Python without clobbering the 64-bit one

If a 64-bit Python already exists (very likely — the backend needs one), install the
32-bit build to a **separate folder** and do not put it on `PATH`:

```powershell
%USERPROFILE%\Downloads\python-3.13.15.exe /passive InstallAllUsers=0 PrependPath=0 `
    Include_launcher=0 TargetDir="$env:LOCALAPPDATA\Programs\Python\Python313-32"
```

That is a per-user install: no admin needed, and it is removable via *Add or remove
programs*. Always call it by full path so it cannot shadow the 64-bit interpreter.

## 2. Configure

Set these as system environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `ZK_PUSH_AGENT_TOKEN` | *(none)* | **Required.** Shared secret. The agent refuses to start without it. |
| `ZK_PULLSDK_DLL` | `plcommpro.dll` | Full path to the DLL, e.g. `C:\PullSDK\plcommpro.dll`. |
| `ZK_PUSH_AGENT_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` only if the backend is on another machine. |
| `ZK_PUSH_AGENT_PORT` | `8081` | Listen port. |
| `ZK_PANEL_PASSWORD` | *(empty)* | Default password for panels that require one. |
| `ZK_AGENT_BUFFER_SIZE` | `65536` | Reply buffer. Raise it for panels with very many users. |

Generate a token with something like:
```powershell
-join ((48..57) + (97..122) | Get-Random -Count 40 | % {[char]$_})
```

## 3. Run

**With the installer** (recommended — see [`installer/README.md`](installer/README.md)):

```powershell
# Administrator. Silent, fleet-wide:
ZkPushAgent-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
    /TOKEN=<secret> /SDK="C:\PullSDK" /HOST=0.0.0.0 /PORT=8081

# Then paste the two PUSH_AGENT_* lines from {app}\BACKEND-SNIPPET.txt into the
# backend's environment variable and restart the backend.
```

**By hand:**

```powershell
& "C:\Python311-32\python.exe" zk_push_agent.py
```

The agent **fails fast** rather than booting half-working:

| Exit | Meaning |
|---|---|
| `2` | `ZK_PUSH_AGENT_TOKEN` is not set. |
| `3` | The DLL could not be loaded (wrong path, or 64-bit Python). |
| — | Running. It warns on stderr if Python is 64-bit. |

Check it:
```powershell
curl http://127.0.0.1:8081/health -H "Authorization: Bearer <token>"
# {"ok": true, "dll": "C:\\PullSDK\\plcommpro.dll", "python_bits": 32, "pull_last_error": 0}
```

### Run it as a service

Task Scheduler (simplest):
1. *Create Task* → **Run whether user is logged on or not**
2. Trigger: **At startup**
3. Action: the `python.exe` above, argument `zk_push_agent.py`, *Start in* = this folder
4. Tick **Restart the task if it fails**, every 1 minute

Or with [NSSM](https://nssm.cc/): `nssm install ZkPushAgent "C:\Python311-32\python.exe" zk_push_agent.py`

The installer does this for you with `app/install_task.ps1`, and it is stricter
than the manual recipe: it uses `-LogonType ServiceAccount` (no stored password),
`-MultipleInstances IgnoreNew` (a failed start does not pile up processes) and an
unlimited `-ExecutionTimeLimit` (the Task Scheduler default would kill the agent
after three days). It also refuses a 64-bit `python.exe` outright.

## 4. Point the backend at it

In the backend's environment:
```
PUSH_AGENT_URL=http://<windows-host>:8081
PUSH_AGENT_TOKEN=<the same token>
```

Then the endpoint stops answering 503 and starts writing:

```bash
# Look first, send nothing:
curl -X POST "http://<backend>/api/devices/1/personnel/sync?dry_run=true"

# Then for real:
curl -X POST "http://<backend>/api/devices/1/personnel/sync"
```

## 5. API

Every request needs `Authorization: Bearer <token>`; without it the answer is 401.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Is the DLL loaded, and is this 32-bit Python? |
| `GET` | `/search?broadcast=192.168.1.255` | UDP scan for panels. |
| `GET` | `/panels/{ip}/tables/{table}?fields=a,b` | Read a device table. |
| `POST` | `/panels/{ip}/tables/{table}` | Write records: `{"records": [{...}]}`. Reads back and returns `verified_records`. |
| `POST` | `/panels/{ip}/tables/{table}` with `"delete": true` | Delete records. |
| `GET` | `/panels/{ip}/params?names=~SerialNumber,FirmVer` | Read device parameters. |
| `POST` | `/panels/{ip}/params` | Set parameters: `{"params": {"DateTime": "..."}}`. |
| `POST` | `/panels/{ip}/control` | `{"operation": 1, "p1": 1}` — relay/door operations. |

Records are encoded exactly as the SDK expects: `Field=value` pairs joined by a
TAB, records joined by CRLF, payload CRLF-terminated. Fields with a `None` value
are dropped rather than sent as the text `None`.

### Door masks

`userauthorize.AuthorizeDoorId` is a **4-door bitmask for that panel**, with door 1
as the least significant bit:

| Doors | Value |
|---|---|
| 1 | 1 |
| 1, 2 | 3 |
| 2, 4 | 10 |
| 3 | 4 |

## 6. Safety

- **This process can open doors** and rewrite who may enter. Treat the token like a
  door key. Bind to a private interface, never expose it to the internet.
- **Test on one panel first.** Nothing here has been run against real hardware yet.
  Use `?dry_run=true` from the backend to see the records before sending them.
- Writing an existing user is an **update**, so fields you do not send may be reset
  by the panel. The backend currently sends `Pin`, `Name`, `CardNo` (and validity
  dates when set) — it does **not** yet preserve `Password`, `Group`,
  `SuperAuthorize` or `Disable`. Verify on a single test user before pushing a
  whole panel, and have ZKAccess or a screenshot as a rollback reference.
- `GetDeviceParam` accepts at most 30 names per call and `SetDeviceParam` at most
  20; the agent already chunks them.

## 7. Troubleshooting

### `Connect()` fails with `PullLastError=-201`

**The number one trap.** `plcommpro.dll` loads fine and reports all its exports, the
panel is reachable, and connect still fails — because the SDK loads its TCP/USB/RS
transport lazily on the first `Connect()`, and resolves those sibling DLLs **relative
to the process working directory**. `os.add_dll_directory()` does *not* help.

Measured on real hardware:

| Way of making the siblings findable | `Connect()` |
|---|---|
| nothing special | ❌ `-201` |
| `os.add_dll_directory(<sdk dir>)` | ❌ `-201` |
| **working directory = SDK folder** | ✅ reads serial + firmware |

The agent already does this (`prepare_sdk_folder()` moves the working directory to
wherever `ZK_PULLSDK_DLL` points), so you only hit this if you drive the DLL yourself.
Two other fixes if you cannot change the working directory: keep every `pl*.dll` in
one folder, or copy them to `C:\Windows\SysWOW64` as ZKTeco's own docs suggest.

### The DLL will not load at all

`Check the VC++ Redistributable x86` (see prerequisites). "Could not find module … or
one of its dependencies" almost always means this, not a wrong path.

### Nothing works and Python says 64-bit

You are running the wrong interpreter. Use the full path to the 32-bit one; note that
`python` on `PATH` is probably still the 64-bit build.

## 8. Verify

```powershell
C:\Python313-32\python.exe check_setup.py --test 10.100.1.12
```

`--test <ip>` opens a real session and reads the serial number, which is the only way
to catch the `-201` trap before it reaches a push. Without it, the check can only
confirm that the DLL loads.

A healthy run:

```
  OK    Python 3.13.15 (32-bit)
  OK    ditemukan: C:\PullSDK\plcommpro.dll
  OK    export Connect: True
  OK    export SetDeviceData: True
  OK    export DeleteDeviceData: True
  OK    export GetDeviceData: True
  OK    konek ke 10.100.1.12: ~SerialNumber=AJYS082162447,FirmVer=AC Ver 5.4.3.2001,LockCount=1
  OK    ZK_PUSH_AGENT_TOKEN terisi (36 karakter)
  OK    Siap. Jalankan:  python zk_push_agent.py
```

## 9. Known gaps

### A push is additive — it never removes anybody

The backend writes `user` and `userauthorize` rows. It does **not** delete rows that
exist on the panel but not in our database. Consequence:

- nobody is locked out by a push (safe to run)
- but somebody who *should* have lost access keeps it, and a panel can hold users
  our data does not account for

Measured against the live fleet with `?dry_run=true`:

| Panel | Users on the panel | Users we would write |
|---|---|---|
| ICU | 387 | 387 |
| RUANGAN SERVER | 41 | 41 |
| RM LT3 | 81 | 81 |
| NICU PICU | 299 | 299 |
| most others | 522 | 523 |
| **OK PETUGAs** | **405** | **361** |

The `+1` on the big panels is the extra test person someone added in the dashboard
(they sit in `AKSES UMUM KARYAWAN`, which reaches those panels) — exactly the
divergence a first push would close.

`OK PETUGAs` is different: it genuinely holds **405** users (verified with a live
read of the panel, not a cached number) while our access levels only account for
**361**. Those 44 extra users would survive a push. Investigate that panel before
pushing to it — it is also the panel whose serial number and lock count this
software cannot read.

Deleting is supported by the agent (`{"records": [...], "delete": true}`) but the
backend deliberately does not use it yet: an accidental mass delete is far worse
than a stale user.

### Other gaps

- Validity dates (`StartTime`/`EndTime`) are encoded as `YYYYMMDD`, which is
  **inferred, not verified** on this fleet. They are NULL for every migrated
  person, so nothing sends them today.
- `Group` is not carried by the backend's schema at all.
- No push for `timezone` yet — access levels keep their `device_timezone_id`, but
  the time zone rows themselves are not written.
