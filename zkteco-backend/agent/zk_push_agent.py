"""Windows push agent for ZKTeco C3 panels.

The main backend (Linux) cannot write to a panel: the pure-Python `zkaccess-c3`
library implements only the read side of the ZKTeco Pull SDK. Writing needs the
official `plcommpro.dll`, which is Windows-only and **32-bit**, so it cannot be
loaded by the 64-bit Python the backend runs on.

This agent is the bridge. It is deliberately tiny and has **no third-party
dependencies** — plain 32-bit CPython plus the official DLL is enough to run it:

    set ZK_PUSH_AGENT_TOKEN=<a long random secret>
    set ZK_PULLSDK_DLL=C:\\PullSDK\\plcommpro.dll
    python zk_push_agent.py            # 32-bit Python!

It exposes the device tables generically, so the backend can write `user`,
`userauthorize`, `timezone` — whatever it needs — without this agent knowing
anything about the schema.

Record encoding (from the official SDK, mirrored by pyzkaccess):
    one record  = "Field1=value1<TAB>Field2=value2"
    a request   = records joined by CRLF, with a trailing CRLF

SECURITY: this process can open doors. It therefore refuses to start without a
token and binds to localhost unless told otherwise.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DLL_PATH = os.environ.get("ZK_PULLSDK_DLL", "plcommpro.dll")
TOKEN = os.environ.get("ZK_PUSH_AGENT_TOKEN", "").strip()
HOST = os.environ.get("ZK_PUSH_AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ZK_PUSH_AGENT_PORT", "8081"))
#: Default password for panels that require one; per-request override allowed.
PANEL_PASSWORD = os.environ.get("ZK_PANEL_PASSWORD", "")
#: 64 KB is comfortable for a few hundred records; the DLL fills it in place.
BUFFER_SIZE = int(os.environ.get("ZK_AGENT_BUFFER_SIZE", str(64 * 1024)))
#: Reading a table back after a write is best-effort. Both -2 (busy) and -112
#: (reply larger than the buffer) turn up intermittently on a panel that is in
#: use, and a failed *read* is not a failed *write*.
READ_BACK_ATTEMPTS = 3
READ_BACK_RETRY_SECONDS = 0.5
#: Panels also fail ordinary reads and connects intermittently (-2 busy, -107
#: connect refused). Measured: a sweep of 20 panels reported two read failures and
#: both passed on an immediate re-run, so one retry is the difference between a
#: real answer and a false alarm.
READ_ATTEMPTS = 3
READ_RETRY_SECONDS = 0.5


class AgentError(RuntimeError):
    """A failure worth reporting to the caller verbatim."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def prepare_sdk_folder(dll_path: str) -> str:
    """Make the SDK's transport plugins findable, and return the DLL's real path.

    `plcommpro.dll` loads its TCP/USB/RS transport lazily, on the first
    Connect(), and it resolves those sibling DLLs **relative to the process
    working directory** — `os.add_dll_directory()` does not help. Verified:
    with the working directory elsewhere, Connect() fails with
    `PullLastError=-201` even though the DLL itself loads fine and the panel is
    reachable.

    This is a process-wide, one-time change, so it happens once at load rather
    than around every call (which would race between the agent's threads).
    """
    absolute = os.path.abspath(dll_path)
    folder = os.path.dirname(absolute)
    if os.path.isdir(folder):
        os.chdir(folder)
        # Return an absolute path so later loads do not depend on the new cwd.
        return absolute
    return dll_path


# --------------------------------------------------------------------------- DLL
class PullSDK:
    """Thin ctypes wrapper over the official Pull SDK.

    Only the calls this agent needs. Signatures follow the SDK (and match
    pyzkaccess, which wraps the same DLL).
    """

    def __init__(self, dll_path: str):
        try:
            # WinDLL => stdcall, which is what the SDK exports.
            self.dll = ctypes.WinDLL(prepare_sdk_folder(dll_path))
        except (OSError, AttributeError) as exc:
            # AgentError, not SystemExit: this can also happen per request, and a
            # BaseException would escape the HTTP handler and kill its thread.
            raise AgentError(
                f"Tidak bisa memuat '{dll_path}': {exc}. Pastikan Pull SDK resmi "
                "ZKTeco sudah dipasang dan Python yang dipakai 32-bit.",
                status=500,
            ) from exc

    def _check(self, err: int, what: str) -> None:
        if err < 0:
            last = self.dll.PullLastError()
            raise AgentError(f"{what} gagal (kode {err}, PullLastError={last})")

    # -- connection ----------------------------------------------------
    def connect(self, ip: str, password: str = "", timeout_ms: int = 4000) -> int:
        conn = (
            f"protocol=TCP,ipaddress={ip},port=4370,timeout={timeout_ms},passwd={password}"
        ).encode()
        handle = self.dll.Connect(conn)
        if not handle:
            raise AgentError(
                f"Tidak bisa konek ke panel {ip} (PullLastError={self.dll.PullLastError()})",
                status=502,
            )
        return handle

    def disconnect(self, handle: int) -> None:
        if handle:
            self.dll.Disconnect(handle)

    # -- data tables ---------------------------------------------------
    def set_table(self, handle: int, table: str, records: list[dict]) -> None:
        if not records:
            return
        self._check(
            self.dll.SetDeviceData(handle, table.encode(), encode_records(records), ""),
            f"SetDeviceData({table})",
        )

    def delete_table(self, handle: int, table: str, records: list[dict]) -> None:
        if not records:
            return
        self._check(
            self.dll.DeleteDeviceData(handle, table.encode(), encode_records(records), ""),
            f"DeleteDeviceData({table})",
        )

    def get_table(self, handle: int, table: str, fields: list[str] | None = None) -> list[dict]:
        buf = ctypes.create_string_buffer(BUFFER_SIZE)
        query_fields = "\t".join(fields).encode() if fields else b"*"
        self._check(
            self.dll.GetDeviceData(
                handle, buf, BUFFER_SIZE, table.encode(), query_fields, b"", b""
            ),
            f"GetDeviceData({table})",
        )
        raw = buf.value.decode("utf-8", errors="ignore")
        lines = raw.split("\r\n")
        if not lines or not lines[0]:
            return []
        headers = lines.pop(0).split(",")
        rows = []
        for line in lines:
            if not line:
                continue
            rows.append(dict(zip(headers, line.split(","), strict=False)))
        if fields:
            keep = set(fields)
            rows = [{k: v for k, v in row.items() if k in keep} for row in rows]
        return rows

    # -- parameters / control ------------------------------------------
    def set_params(self, handle: int, params: dict) -> None:
        """Set device parameters. The SDK accepts at most 20 per call."""
        if not params:
            return
        keys = sorted(params)
        for start in range(0, len(keys), 20):
            chunk = keys[start : start + 20]
            query = ",".join(f"{k}={params[k]}" for k in chunk).encode()
            self._check(self.dll.SetDeviceParam(handle, query), "SetDeviceParam")

    def get_params(self, handle: int, names: list[str]) -> dict:
        """Read device parameters. The SDK accepts at most 30 per call."""
        results: dict[str, str] = {}
        for start in range(0, len(names), 30):
            chunk = names[start : start + 30]
            buf = ctypes.create_string_buffer(BUFFER_SIZE)
            query = ",".join(chunk).encode()
            self._check(
                self.dll.GetDeviceParam(handle, buf, BUFFER_SIZE, query),
                "GetDeviceParam",
            )
            for pair in buf.value.decode("utf-8", errors="ignore").split(","):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    results[key] = value
        return results

    def control(self, handle: int, operation: int, p1: int, p2: int, p3: int, p4: int) -> int:
        err = self.dll.ControlDevice(handle, operation, p1, p2, p3, p4, "")
        self._check(err, f"ControlDevice({operation})")
        return err

    def search(self, broadcast: str) -> list[str]:
        buf = ctypes.create_string_buffer(BUFFER_SIZE)
        self._check(self.dll.SearchDevice(b"UDP", broadcast.encode(), buf), "SearchDevice")
        raw = buf.value.decode("utf-8", errors="ignore")
        return [line for line in raw.split("\r\n") if line]


def encode_records(records: list[dict]) -> bytes:
    """Encode records the way `SetDeviceData`/`DeleteDeviceData` expect them.

    One record is `Field=value` pairs joined by TAB; records are joined by CRLF
    and the whole thing ends with a CRLF. Verified against the official SDK's
    usage (and pyzkaccess, which wraps the same DLL).
    """
    lines = [
        "\t".join(f"{k}={v}" for k, v in record.items() if v is not None) for record in records
    ]
    return ("\r\n".join(lines) + "\r\n").encode()


SDK: PullSDK | None = None


def get_sdk() -> PullSDK:
    """Load the DLL on first use.

    Deliberately lazy: loading at import time would make this module impossible
    to import (and therefore to unit-test) on a machine without the DLL —
    including the Linux CI that only needs `encode_records`.
    """
    global SDK  # noqa: PLW0603 - one process-wide DLL handle is the point
    if SDK is None:
        SDK = PullSDK(DLL_PATH)
    return SDK


# ----------------------------------------------------------------- HTTP layer
def _retry(fn, *, attempts: int = READ_ATTEMPTS, delay: float = READ_RETRY_SECONDS):
    """Run `fn`, retrying the transient failures panels produce.

    The last error is re-raised, so a panel that is genuinely unreachable still
    reports as unreachable rather than being retried into silence.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except AgentError as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
    raise last  # type: ignore[misc] - the loop always sets it before falling out


def _read_back(
    sdk: PullSDK, handle: int, table: str, fields: list[str]
) -> tuple[list[dict] | None, str | None]:
    """Read a table back after a write. Returns ``(rows, error)``.

    Best-effort by design: the write has already succeeded by the time this runs,
    so a read that fails must not be reported as a failed write.
    """
    try:
        return _retry(lambda: sdk.get_table(handle, table, fields)), None
    except AgentError as exc:
        return None, str(exc)


def _handle(method: str, parts: list[str], body: dict, query: dict) -> object:
    """Dispatch one request. `parts` is the path split on '/'."""
    if parts == ["health"]:
        sdk = get_sdk()
        return {
            "ok": True,
            "dll": DLL_PATH,
            "python_bits": 64 if sys.maxsize > 2**32 else 32,
            "pull_last_error": sdk.dll.PullLastError(),
        }

    if parts == ["search"]:
        broadcast = query.get("broadcast", ["255.255.255.255"])[0]
        return {"panels": get_sdk().search(broadcast)}

    if len(parts) >= 2 and parts[0] == "panels":
        ip = parts[1]
        password = body.get("password") or PANEL_PASSWORD
        sdk = get_sdk()
        # A full `transaction` table is several megabytes (57k rows on 10.100.1.3). The
        # default connect timeout expires mid-transfer and the SDK reports -2, which
        # reads exactly like a busy/broken panel. A caller that knows it is reading a
        # big table asks for a longer one.
        timeout_ms = int(query.get("timeout_ms", ["4000"])[0])
        handle = _retry(lambda: sdk.connect(ip, password, timeout_ms))
        try:
            return _panel_op(sdk, parts[2:], handle, body, query)
        finally:
            sdk.disconnect(handle)

    raise AgentError(f"Path tidak dikenal: /{'/'.join(parts)}", status=404)


def _panel_op(sdk: PullSDK, parts: list[str], handle: int, body: dict, query: dict) -> object:
    if not parts:
        raise AgentError("Tentukan operasi setelah /panels/{ip}", status=404)

    if parts[0] == "tables":
        if len(parts) < 2:
            raise AgentError("Sebutkan nama tabel", status=404)
        table = parts[1]
        if not body.get("records") and "records" not in body:
            # GET
            fields = query.get("fields", [None])[0]
            field_list = fields.split(",") if fields else None
            rows = _retry(lambda: sdk.get_table(handle, table, field_list))
            return {"table": table, "count": len(rows), "records": rows}
        records = body["records"]
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise AgentError("'records' harus list of object")
        if body.get("delete"):
            sdk.delete_table(handle, table, records)
            return {"table": table, "deleted": len(records)}
        sdk.set_table(handle, table, records)
        # Read back, so the caller can verify instead of trusting the write. Kept
        # separate from the write itself: failing to *read* is not failing to
        # *write*, and conflating the two told operators a panel had not been
        # updated when it already held the record (seen as "Gagal: IGD 2" on a
        # panel whose user list did contain the new Pin).
        fields = [k for record in records[:1] for k in record][:30]
        verified, verify_error = _read_back(sdk, handle, table, fields)
        result: dict = {"table": table, "written": len(records), "verified_records": verified}
        if verify_error:
            result["verify_error"] = verify_error
        return result

    if parts[0] == "params":
        if "params" in body:
            sdk.set_params(handle, body["params"])
            return {"set": len(body["params"])}
        names = (query.get("names", [""])[0] or "").split(",")
        return {"params": sdk.get_params(handle, [n for n in names if n])}

    if parts[0] == "control":
        operation = int(body["operation"])
        result = sdk.control(
            handle,
            operation,
            int(body.get("p1", 1)),
            int(body.get("p2", 0)),
            int(body.get("p3", 0)),
            int(body.get("p4", 0)),
        )
        return {"operation": operation, "result": result}

    raise AgentError(f"Operasi tidak dikenal: {parts[0]}", status=404)


class Handler(BaseHTTPRequestHandler):
    server_version = "ZkPushAgent/1.0"

    def _reply(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        if not self._authorised():
            self._reply(401, {"error": "Authorization: Bearer <token> wajib"})
            return

        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)

        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            self._reply(400, {"error": f"JSON tidak valid: {exc}"})
            return

        try:
            self._reply(200, _handle(method, parts, body, query))
        except AgentError as exc:
            self._reply(exc.status, {"error": str(exc)})
        except KeyError as exc:
            self._reply(400, {"error": f"Field wajib tidak ada: {exc}"})
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to a panel
            self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args) -> None:
        # stderr, so the output lands in the service log. The %-style format
        # string is the stdlib's own logging protocol.
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")  # noqa: UP031


def main() -> int:
    if not TOKEN:
        print(
            "TOLAK JALAN: set ZK_PUSH_AGENT_TOKEN dulu.\n"
            "Agen ini bisa membuka pintu, jadi tidak boleh tanpa token.",
            file=sys.stderr,
        )
        return 2
    if sys.maxsize <= 2**32:
        print("Python 32-bit terdeteksi. Benar.", file=sys.stderr)
    else:
        print(
            "PERINGATAN: Python ini 64-bit. plcommpro.dll adalah 32-bit dan "
            "kemungkinan besar gagal dimuat.",
            file=sys.stderr,
        )

    print(f"ZkPushAgent di http://{HOST}:{PORT}  (dll={DLL_PATH})", file=sys.stderr)
    # Fail fast and clearly: booting a server that cannot load the DLL would only
    # surface the problem as a confusing 500 on the first real push.
    try:
        get_sdk()
    except AgentError as exc:
        print(f"TOLAK JALAN: {exc}", file=sys.stderr)
        return 3

    # Threaded: the backend pushes to several panels in parallel.
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
