"""The Windows push agent itself.

`agent/zk_push_agent.py` is what actually writes to a panel, so its HTTP surface
is tested for real: a server is started on a random port with the DLL-backed SDK
replaced by a recorder, and requests go over the wire.

This is also the only place that pins the record encoding the official SDK
expects, because getting that wrong corrupts a live panel instead of failing.
"""

from __future__ import annotations

import os
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from agent import zk_push_agent

TOKEN = "test-token"


class FakePanel:
    """Stands in for the DLL-backed SDK and remembers what it was asked to do."""

    def __init__(self):
        self.written: list[tuple[str, list[dict]]] = []
        self.deleted: list[tuple[str, list[dict]]] = []
        self.controls: list[tuple] = []
        self.connected_to: list[str] = []
        #: How many `get_table` calls should fail before one succeeds, so the
        #: transient read errors a busy panel produces can be reproduced.
        self.get_failures = 0
        self.get_calls = 0
        self.connect_failures = 0
        self.connect_calls = 0
        self.dll = types.SimpleNamespace(PullLastError=lambda: 0)

    def connect(self, ip, password="", timeout_ms=4000):  # noqa: ARG002
        self.connect_calls += 1
        if self.connect_failures:
            self.connect_failures -= 1
            raise zk_push_agent.AgentError(
                f"Tidak bisa konek ke panel {ip} (PullLastError=-107)", status=502
            )
        self.connected_to.append(ip)
        return 123

    def disconnect(self, handle):  # noqa: ARG002 - mirrors the real signature
        pass

    def set_table(self, handle, table, records):  # noqa: ARG002
        self.written.append((table, records))

    def delete_table(self, handle, table, records):  # noqa: ARG002
        self.deleted.append((table, records))

    def get_table(self, handle, table, fields=None):  # noqa: ARG002
        self.get_calls += 1
        if self.get_failures:
            self.get_failures -= 1
            raise zk_push_agent.AgentError("GetDeviceData(user) gagal (kode -2)")
        return [{"Pin": "1001", "CardNo": "5001"}]

    def control(self, handle, operation, p1, p2, p3, p4):  # noqa: ARG002
        self.controls.append((operation, p1, p2, p3, p4))
        return 0


@pytest.fixture
def agent(monkeypatch):
    """The agent module, with a fixed token and a recorder instead of the DLL."""
    fake = FakePanel()
    monkeypatch.setattr(zk_push_agent, "TOKEN", TOKEN)
    monkeypatch.setattr(zk_push_agent, "get_sdk", lambda: fake)
    return zk_push_agent, fake


@pytest.fixture
def live_agent(agent):
    """A real HTTP server on a free port, so routing and auth are exercised."""
    module, fake = agent
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


# -- the wire format -------------------------------------------------------
def test_encodes_records_the_way_the_sdk_expects():
    """Fields joined by TAB, records by CRLF, whole payload CRLF-terminated."""
    encoded = zk_push_agent.encode_records(
        [{"Pin": "1001", "CardNo": 5001}, {"Pin": "1002", "Name": "Ani"}]
    )

    assert encoded == b"Pin=1001\tCardNo=5001\r\nPin=1002\tName=Ani\r\n"


def test_encodes_nothing_for_unset_fields():
    """A None would be written as the literal text 'None' on the panel."""
    assert zk_push_agent.encode_records([{"Pin": "1001", "Name": None}]) == b"Pin=1001\r\n"


# -- finding the SDK's transport plugins -----------------------------------
def test_prepare_sdk_folder_moves_cwd_so_transports_are_found(tmp_path):
    """The SDK loads its TCP/USB/RS transport relative to the *working directory*.

    Verified against real hardware: with the working directory elsewhere,
    Connect() fails with `PullLastError=-201` even though the DLL loads fine and
    the panel is reachable. `os.add_dll_directory()` does not help; a chdir does.
    """
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    dll = sdk / "plcommpro.dll"
    dll.write_bytes(b"")
    (sdk / "pltcpcomm.dll").write_bytes(b"")

    original = os.getcwd()
    try:
        returned = zk_push_agent.prepare_sdk_folder(str(dll))

        assert Path(os.getcwd()) == sdk
        # Returns an absolute path so later loads do not depend on the new cwd.
        assert Path(returned) == dll.resolve()
    finally:
        os.chdir(original)


def test_prepare_sdk_folder_resolves_a_bare_name_against_the_cwd():
    """A bare 'plcommpro.dll' has no folder of its own, so the cwd becomes it.

    The returned path is absolute either way, so the load that follows cannot
    depend on the working directory we just changed.
    """
    original = os.getcwd()
    try:
        returned = zk_push_agent.prepare_sdk_folder("plcommpro.dll")

        assert Path(returned).is_absolute()
        assert Path(returned).parent == Path(original)
        assert os.getcwd() == original  # a chdir to where we already were
    finally:
        os.chdir(original)


# -- authentication --------------------------------------------------------
def test_agent_rejects_a_request_without_a_token(live_agent):
    base, _ = live_agent

    response = httpx.get(f"{base}/health")

    assert response.status_code == 401
    assert "token" in response.json()["error"].lower()


def test_agent_rejects_a_wrong_token(live_agent):
    base, _ = live_agent

    response = httpx.get(f"{base}/health", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


def test_health_reports_the_agent_is_alive(live_agent):
    base, _ = live_agent

    body = httpx.get(f"{base}/health", headers=AUTH).json()

    assert body["ok"] is True
    assert body["python_bits"] in (32, 64)


# -- writing ---------------------------------------------------------------
def test_writing_a_table_connects_writes_and_reads_back(live_agent):
    base, fake = live_agent
    records = [{"Pin": "1001", "CardNo": 5001}]

    body = httpx.post(
        f"{base}/panels/10.100.1.14/tables/user",
        headers=AUTH,
        json={"records": records},
    ).json()

    assert fake.connected_to == ["10.100.1.14"]
    assert fake.written == [("user", records)]
    assert body["written"] == 1
    # The agent reads the table back so the caller does not have to trust the write.
    assert body["verified_records"] == [{"Pin": "1001", "CardNo": "5001"}]


def test_a_transient_read_back_failure_is_not_reported_as_a_failed_write(live_agent):
    """The write is what matters; the read-back is only a convenience.

    Panels answer -2 (busy) or -112 (reply larger than the buffer) intermittently.
    Treating that as a failed write told operators a panel had not been updated
    when it already held the new record — seen as "Gagal: IGD 2" on a panel whose
    user list did contain the new Pin.
    """
    base, fake = live_agent
    fake.get_failures = 99  # every read-back attempt fails

    response = httpx.post(
        f"{base}/panels/10.100.1.14/tables/user",
        headers=AUTH,
        json={"records": [{"Pin": "2150141426", "CardNo": 2150141426}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["written"] == 1
    assert fake.written == [("user", [{"Pin": "2150141426", "CardNo": 2150141426}])]
    # "Could not verify" is reported as such, and is not the same as "did not write".
    assert body["verified_records"] is None
    assert "-2" in body["verify_error"]


def test_read_back_is_retried_before_giving_up(live_agent):
    base, fake = live_agent
    fake.get_failures = 1  # first attempt fails, the retry succeeds

    body = httpx.post(
        f"{base}/panels/10.100.1.14/tables/user",
        headers=AUTH,
        json={"records": [{"Pin": "1001"}]},
    ).json()

    assert fake.get_calls == 2
    assert body["verified_records"] == [{"Pin": "1001", "CardNo": "5001"}]
    assert "verify_error" not in body


def test_deleting_records_uses_the_delete_call(live_agent):
    base, fake = live_agent

    body = httpx.post(
        f"{base}/panels/10.100.1.14/tables/userauthorize",
        headers=AUTH,
        json={"records": [{"Pin": "1001"}], "delete": True},
    ).json()

    assert fake.deleted == [("userauthorize", [{"Pin": "1001"}])]
    assert fake.written == []
    assert body["deleted"] == 1


def test_reading_a_table_returns_records(live_agent):
    base, fake = live_agent

    body = httpx.get(f"{base}/panels/10.100.1.14/tables/user", headers=AUTH).json()

    assert body["count"] == 1
    assert body["records"][0]["Pin"] == "1001"
    assert fake.written == []


def test_reading_a_table_retries_a_transient_failure(live_agent):
    """A -2/-107 read is normally transient; report it only if it really persists."""
    base, fake = live_agent
    fake.get_failures = 1

    response = httpx.get(f"{base}/panels/10.100.1.14/tables/user", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert fake.get_calls == 2


def test_a_persistent_read_failure_is_still_reported(live_agent):
    """Retrying must not turn a genuinely unreachable panel into a success."""
    base, fake = live_agent
    fake.get_failures = 99

    response = httpx.get(f"{base}/panels/10.100.1.14/tables/user", headers=AUTH)

    assert response.status_code == 400
    assert "-2" in response.json()["error"]


def test_connecting_is_retried_before_giving_up(live_agent):
    base, fake = live_agent
    fake.connect_failures = 1

    response = httpx.get(f"{base}/panels/10.100.1.14/tables/user", headers=AUTH)

    assert response.status_code == 200
    assert fake.connect_calls == 2
    assert fake.connected_to == ["10.100.1.14"]


def test_control_switches_a_relay(live_agent):
    base, fake = live_agent

    body = httpx.post(
        f"{base}/panels/10.100.1.14/control",
        headers=AUTH,
        json={"operation": 1, "p1": 2},
    ).json()

    assert body["operation"] == 1
    assert fake.controls == [(1, 2, 0, 0, 0)]


# -- refusals --------------------------------------------------------------
def test_records_must_be_a_list_of_objects(live_agent):
    base, fake = live_agent

    response = httpx.post(
        f"{base}/panels/10.100.1.14/tables/user",
        headers=AUTH,
        json={"records": ["not-a-record"]},
    )

    assert response.status_code == 400
    assert fake.written == []


def test_an_unknown_path_is_a_404(live_agent):
    base, _ = live_agent

    response = httpx.get(f"{base}/nonsense", headers=AUTH)

    assert response.status_code == 404


def test_a_panel_that_will_not_answer_is_a_502(agent, live_agent):
    """A panel that refuses the connection is a gateway problem, not a client error."""
    module, fake = agent
    base, _ = live_agent

    def refuse(ip, password="", timeout_ms=4000):  # noqa: ARG001
        raise module.AgentError(f"Tidak bisa konek ke panel {ip}", status=502)

    fake.connect = refuse

    response = httpx.get(f"{base}/panels/10.100.1.14/tables/user", headers=AUTH)

    assert response.status_code == 502
    assert "Tidak bisa konek" in response.json()["error"]
