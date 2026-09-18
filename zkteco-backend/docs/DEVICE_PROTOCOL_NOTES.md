# Device protocol notes — findings against real C3 panels

Everything below was reproduced against the hospital's live panels
(23 × C3-100/C3-200, firmware `AC Ver 5.4.3.2001`, builds Sep 2019 – Feb 2025)
using `zkaccess-c3` 0.0.15 on Python 3.13.

> ## ⚠️ SUPERSEDED IN PART — read this before believing §1
>
> This file is the **historical record of the protocol investigation**. Some of it is
> still very useful (the firmware defects, the `transaction` diagnosis, the door/realtime
> commands), but **§1 is out of date**.
>
> Writing to the panels **is implemented and proven**: the official 32-bit Pull SDK
> (`plcommpro.dll`) runs inside a separate Windows agent (`agent/zk_push_agent.py`) that
> the Linux backend calls over HTTP. So `POST /api/devices/{id}/personnel/sync` performs
> a real read-compare-write, and it answers **503** — never 501 — when `PUSH_AGENT_URL`
> is not configured.
>
> The official SDK wrap-up in §1 under "Options" is what was actually built (option 1),
> on top of the write-record format in option 2 rather than SETDATA itself.
>
> For the current truth see [`../../AGENTS.md`](../../AGENTS.md) and [`../README.md`](../README.md).

## 1. The library is read-only (historical)

`c3.consts.Command` contains only:

```
CONNECT_SESSION_LESS 1   DISCONNECT 2      DATETIME 3
GETPARAM 4               CONTROL 5         DATATABLE_CFG 6
GETDATA 8                RTLOG_BINARY 11   DISCOVER 20
CONNECT_SESSION 118      RTLOG_KEYVALUE 121
```

There is **no `SETDATA`/write command**. Consequences:

| Feature requested | Status |
|---|---|
| Device CRUD, Get Information, health, discover | works |
| Read personnel from device, count personnel | works |
| Read access logs / transaction table | partially (see §3) |
| Realtime events (`get_rt_log`) | works |
| Open door / cancel alarm / restart / normal-open | works |
| Set device date-time | works |
| **Push personnel to device ("Sync All Data to Device")** | **not possible *with this library*** — done by the Windows agent instead (implemented, proven on hardware) |
| **Delete user on device** | deliberately **not done**: a push is additive for `user`; only `userauthorize` rights are revoked |
| **Write timezones / access groups to device** | partly: `userauthorize` rights are pushed, `timezone` rows are not sent yet |

`POST /api/devices/{id}/personnel/sync` originally answered **501 Not Implemented**.
Today it runs a full read → diff → write cycle through the agent, and **503** means
"agent not configured" (`PUSH_AGENT_URL` empty).

Options if pushing data is a hard requirement:

1. **ZKAccess C3 SDK (official, Windows COM/DLL)** — supports write; needs a
   Windows host and a licensed SDK, and it is the component with the 25-door /
   2000-user limit.
2. **Implement `SETDATA` yourself** — the protocol frames are already handled in
   `c3/core.py` (`_construct_message`, `_send_receive`); only the command
   constant and the record encoder are missing. Doable but must be validated on
   a spare panel: a wrong write corrupts user data.
3. **Keep ZKAccess for provisioning, use this backend for reading/audit** —
   lowest risk, and it still removes the read-side limits.

**Decision taken (2026-09-16): option 1.** The write path is
[`agent/zk_push_agent.py`](../agent/zk_push_agent.py) + the official
`plcommpro.dll`, and it was verified against live hardware. Note that the panel DOES
expose a write command — `c3.consts.Command` simply jumps from `DATATABLE_CFG = 0x06`
to `GETDATA = 0x08`, omitting **`0x07` (SETDATA)**. Implementations 1 and 2 are
compatible: the agent writes whole records (`Key=Value` pairs, tab-separated, one
record per CRLF line) because `SetDeviceData` **replaces** a record — any field left
out is cleared, which is why `panel_push._carry_over()` copies the panel's own
`Password/Group/StartTime/EndTime/SuperAuthorize/Disable` values back onto every
rewritten record.

## 2. `DATATABLE_CFG` parsing bug (fixed in `app/services/c3_compat.py`)

These panels append an extra configuration line:

```
logfmt=256,logfmt=i1
```

`c3.core._parse_kv_from_message` returns a `dict`, so the duplicate `logfmt`
key collapses and the table index becomes the *string* `"i1"`.
`_DataTableCfg.__init__` then calls `int("i1")` and raises:

```
ValueError: invalid literal for int() with base 10: 'i1'
```

Because `get_device_data()` parses the whole configuration before reading any
table, this one bad line made **every** table read fail — `user`,
`transaction`, `holiday`, `timezone`, … all of them.

Fix: skip config lines whose index is not numeric. Applied automatically by
`apply_patches()`.

## 3. Multi-field `GETDATA` is refused (worked around)

After fixing §2, `get_device_data` still behaves inconsistently:

| Fields requested | Result |
|---|---|
| 1 | OK |
| 2 | OK |
| ≥ 3 | 17-byte payload whose first byte is `0` instead of the table index |

The library reports this as `ValueError: Wrong table returned by panel.
Expected 1, received 0`. The 17-byte payload is a genuine reply (no second
message follows), so it is not a lost/misaligned packet.

Workaround in `read_table_robust()`: read one field at a time and merge the
records by position. Verified against real panels:

```
10.100.1.3  user-counts (3 attempts) = [525, 525, 525]
10.100.1.11 user-counts (3 attempts) = [387, 387, 387]
10.100.1.24 user-counts (3 attempts) = [522, 522, 522]
10.100.1.26 user-counts (3 attempts) = [522, 522, 522]
```

The `UID` payload was parsed by hand to confirm the library's count is right:
1320 data bytes → 525 records (255 one-byte UIDs + 270 two-byte UIDs), zero
leftover bytes. So **525 is the true number of users** on 10.100.1.3, and
`~MaxUserCount` (300) is only a per-device capacity figure, not a count.

## 4. Reply framing loses bytes on large payloads (fixed in `c3_compat.py`)

`c3.core.C3._receive` reads the reply payload with a single
`sock.recv(data_size + 3)`. TCP `recv` returns *up to* the requested count, so a
reply that spans more than one segment arrives short and parsing aborts:

```
ValueError: Payload does not include message end marker (32)
```

Being size-dependent, this looks like a per-device quirk rather than a bug:

| Panel | users on device | `userauthorize` before the fix |
|---|---|---|
| 10.100.1.21 | 27 | OK — 27 rows |
| 10.100.1.3 | 525 | `Payload does not include message end marker (32)` |
| 10.100.1.24 | 522 | failed |

The fix reads the 5-byte header and the `data_size + 3` tail with exact-length
loops, keeping the upstream checksum and error-reply validation. After it,
`userauthorize` and `timezone` read correctly on every panel tested:

```
10.100.1.3  users=525  userauthorize=523 rows  timezone=1 row
10.100.1.21 users=27   userauthorize=27 rows   timezone=1 row
10.100.1.24 users=522  userauthorize=522 rows  timezone=1 row
```

This one matters most in practice: `userauthorize` is the **door ↔ person
access matrix** (`Pin`, `AuthorizeTimezoneId`, `AuthorizeDoorId`). It is now
readable, so the per-door access rules can be reconstructed from the panels even
though the library cannot write them.

## 5. `transaction` table is always refused

`get_device_data("transaction", [...])` returns the 17-byte non-data reply on
every panel tested, with one field or many:

```
Pin:          ValueError: Wrong table returned by panel. Expected 5, received 0
Time_second:  ValueError: Wrong table returned by panel. Expected 5, received 0
EventType:    ValueError: Wrong table returned by panel. Expected 5, received 0
```

The table *is* advertised (index 5, 9 fields) and `logfmt` is the table that
failed to parse in §2, which hints the panel's log format descriptor and its
transaction table are inconsistent.

`GET /api/logs/devices/{id}/pull` therefore records a per-device error and
returns `inserted: 0` instead of throwing, so a fleet-wide poll still succeeds
for the panels that do work. Realtime monitoring via
`GET /api/logs/devices/{id}/realtime` is a separate path (`RTLOG_BINARY`) and
returns `[]` when the panel is idle.

**This needs a decision before the Access Log feature can be called complete.**
Next steps to try, in order: capture a real door event with a packet sniffer to
see what the panel pushes; test `RTLOG_KEYVALUE` instead of `RTLOG_BINARY`; or
pull logs with the official Windows SDK as a scheduled side-channel.

## 6. Field types the parser silently drops

`transaction.Cardno` has type `L` (long). `c3.core.get_device_data` handles only
`i` and `s`; its `else` branch constructs a `ValueError` but never raises it, so
`L`-typed fields are dropped from the result without any error. Card numbers
from realtime events are therefore the more reliable source for card data.

## 7. Other observations

* `serial_number`, `device_name`, `mac` return the literal `"?"` when the panel
  has none set. Normalised to `None` in `device_client._clean`.
* `~DeviceName` on most panels is just the model string (`"C3-100"`), so the
  name you configure in this system is more useful than the one on the panel.
* `LockCount` is the reliable way to infer the model: 1 → C3-100,
  2 → C3-200, 4 → C3-300/C3-400.
* Port 4370 was reachable from the app host on all 23 panels; no firewall
  changes were needed.
