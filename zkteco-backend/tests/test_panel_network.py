"""Changing a panel's own network configuration — the "Modify IP Address" feature.

Nothing here touches real hardware: the panel reads/writes go through
`FakeDeviceClient` (autouse fixture) and the agent through a stubbed HTTP transport.

The point of these tests is the **guard**, not the happy path: this is the only
operation in the system that can leave a panel unreachable with no way back, so what
matters is that nothing is written without being switched on, confirmed twice, and
verified afterwards on the new address.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import settings
from app.models import Device
from app.schemas import PanelNetworkUpdate
from app.services import panel_network
from app.services.panel_network import (
    NetworkChangeError,
    PanelNetworkService,
    PanelNetworkWriteDisabled,
)
from app.services.push_agent import PushAgentClient


def stub_agent(monkeypatch, *, status: int = 200, payload: dict | None = None) -> list[dict]:
    """Capture what would be sent to the Windows agent, without one running."""
    calls: list[dict] = []

    class FakeResponse:
        status_code = status
        content = b"{}"
        text = ""

        def json(self):
            return payload or {}

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(httpx, "request", fake_request)
    return calls


def agent_factory():
    return PushAgentClient("http://win-agent:8081", token="t")


def make_device(db, **overrides) -> Device:
    payload = {"name": "RUANGAN SERVER", "ip": "10.100.1.14", "port": 4370}
    payload.update(overrides)
    device = Device(**payload)
    db.add(device)
    db.commit()
    return device


def service(db) -> PanelNetworkService:
    return PanelNetworkService(db, agent_factory=agent_factory)


@pytest.fixture
def network_enabled(monkeypatch):
    """Switch the feature on for the test, and make verification instant."""
    monkeypatch.setattr(settings, "panel_network_write_enabled", True)
    monkeypatch.setattr(panel_network, "VERIFY_DELAY_SECONDS", 0)


def update(**overrides) -> PanelNetworkUpdate:
    payload = {"ip": "10.100.1.30", "netmask": "255.255.255.0", "gateway": ""}
    payload.update(overrides)
    return PanelNetworkUpdate(**payload)


def report_network(
    monkeypatch, fake_clients, *, ip: str, netmask: str, gateway: str = "", mac: str = ""
):
    """Make the fake panel report a specific network configuration (its own values)."""
    from app.services.device_client import DeviceInfo

    def fake_get_info(self, *, include_personnel_count=True):
        return DeviceInfo(
            serial_number=f"TEST{int(self.ip.split('.')[-1]):08d}",
            mac_address=mac,
            raw={
                "IPAddress": ip,
                "NetMask": netmask,
                # The real firmware omits GATEWAY entirely, hence the empty default.
                "GATEWAY": gateway,
                "MAC": mac,
            },
        )

    monkeypatch.setattr(fake_clients, "get_info", fake_get_info)


# -- switching it on --------------------------------------------------------
def test_writing_is_refused_while_the_feature_is_off(db, monkeypatch):
    """Default-off is the whole safety story: no code path may write regardless."""
    monkeypatch.setattr(settings, "panel_network_write_enabled", False)
    device = make_device(db)

    with pytest.raises(PanelNetworkWriteDisabled, match="PANEL_NETWORK_WRITE_ENABLED"):
        service(db).apply(device.id, update(), dry_run=False)


def test_a_dry_run_works_even_while_writing_is_off(db, monkeypatch):
    """Seeing what *would* be sent is how an operator decides to switch it on."""
    monkeypatch.setattr(settings, "panel_network_write_enabled", False)
    device = make_device(db)
    calls = stub_agent(monkeypatch)

    change = service(db).apply(device.id, update(), dry_run=True)

    assert change.enabled is False
    assert change.written is False
    assert change.params["IPAddress"] == "10.100.1.30"
    assert calls == []  # nothing was sent anywhere
    assert any("belum diaktifkan" in warning for warning in change.warnings)


def test_the_read_reports_whether_writing_is_even_possible(db, monkeypatch):
    """The operator must learn that writing is off BEFORE pressing the write button.

    Otherwise the only feedback is a refusal after they have filled the form and typed
    a confirmation — which reads as "the feature is broken" rather than "this server was
    started without the switch".
    """
    device = make_device(db)

    monkeypatch.setattr(settings, "panel_network_write_enabled", False)
    assert service(db).read(device.id).write_enabled is False

    monkeypatch.setattr(settings, "panel_network_write_enabled", True)
    assert service(db).read(device.id).write_enabled is True


# -- the plan itself --------------------------------------------------------
def test_the_dry_run_never_reaches_the_agent(db, network_enabled, monkeypatch):
    device = make_device(db)
    calls = stub_agent(monkeypatch)

    change = service(db).apply(device.id, update(), dry_run=True)

    assert calls == []
    assert change.dry_run is True
    assert change.written is False
    assert change.reachable_at_new_address is None
    assert db.get(Device, device.id).ip == "10.100.1.14"  # record untouched


def test_the_write_carries_the_whole_configuration(db, network_enabled, monkeypatch, fake_clients):
    """`SetDeviceParam` may clear what it is not given, so nothing is left out.

    Whether it actually behaves like `SetDeviceData` (replace, not patch) is unknown —
    which is exactly why the MAC read from the panel is sent back unchanged instead of
    being omitted, and why the netmask cannot be optional.
    """
    device = make_device(db)
    report_network(
        monkeypatch,
        fake_clients,
        ip="10.100.1.14",
        netmask="255.255.255.0",
        mac="00:11:22:33:44:55",
    )
    calls = stub_agent(monkeypatch, payload={"set": 4})

    change = service(db).apply(device.id, update(gateway="10.100.1.1"), dry_run=False)

    assert change.written is True
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://win-agent:8081/panels/10.100.1.14/params"
    assert calls[0]["json"] == {
        "params": {
            "IPAddress": "10.100.1.30",
            "NetMask": "255.255.255.0",
            "GATEWAY": "10.100.1.1",
            "MAC": "00:11:22:33:44:55",
        }
    }


# -- verification and the stored address -----------------------------------
def test_the_record_is_updated_only_when_the_new_address_answers(
    db, network_enabled, monkeypatch, fake_clients
):
    device = make_device(db)
    stub_agent(monkeypatch, payload={"set": 4})

    change = service(db).apply(device.id, update(), dry_run=False)

    assert change.reachable_at_new_address is True
    assert change.record_updated is True
    assert db.get(Device, device.id).ip == "10.100.1.30"


def test_a_panel_that_does_not_answer_on_the_new_address_keeps_the_old_record(
    db, network_enabled, monkeypatch, fake_clients
):
    """The write is gone but unverifiable — say so loudly, and change nothing else.

    Moving the stored address on trust would hide the problem behind a device that
    looks healthy while nothing can reach it.
    """
    device = make_device(db)
    fake_clients.online["10.100.1.30"] = False
    calls = stub_agent(monkeypatch, payload={"set": 4})

    change = service(db).apply(device.id, update(), dry_run=False)

    assert len(calls) == 1  # the write did happen
    assert change.written is True
    assert change.reachable_at_new_address is False
    assert change.record_updated is False
    assert db.get(Device, device.id).ip == "10.100.1.14"
    assert any("TIDAK menjawab" in warning for warning in change.warnings)


def test_verification_retries_because_one_failure_proves_nothing(
    db, network_enabled, monkeypatch, fake_clients
):
    """A panel refuses concurrent connections; a single miss is not a dead panel."""
    device = make_device(db)
    stub_agent(monkeypatch, payload={"set": 4})
    seen: list[str] = []
    real_health = fake_clients.health_check

    def flaky_health(self):
        seen.append(self.ip)
        if len(seen) < 2:
            from app.services.device_client import HealthResult

            return HealthResult(online=False, error="busy (-2)")
        return real_health(self)

    monkeypatch.setattr(fake_clients, "health_check", flaky_health)

    change = service(db).apply(device.id, update(), dry_run=False)

    assert len(seen) == 2
    assert change.reachable_at_new_address is True


# -- validation -------------------------------------------------------------
def test_an_address_outside_the_allowed_subnets_is_refused(db, network_enabled):
    """A panel sent outside the reachable subnets could never be dialled again."""
    device = make_device(db)

    with pytest.raises(NetworkChangeError, match="SCAN_ALLOWED_NETWORKS"):
        service(db).apply(device.id, update(ip="192.168.9.30"), dry_run=True)


def test_a_netmask_that_is_not_a_netmask_is_refused(db, network_enabled):
    device = make_device(db)

    with pytest.raises(NetworkChangeError, match="NetMask"):
        service(db).apply(device.id, update(netmask="255.0.255.0"), dry_run=True)


def test_an_address_already_used_by_another_device_is_refused(db, network_enabled):
    device = make_device(db)
    make_device(db, name="IGD", ip="10.100.1.30")

    with pytest.raises(NetworkChangeError, match="sudah dipakai"):
        service(db).apply(device.id, update(), dry_run=True)


def test_the_panel_that_answers_must_be_the_device_we_think_it_is(db, network_enabled):
    """If the address was moved to another panel, re-addressing it would hit the wrong one."""
    device = make_device(db, serial_number="SERIAL-LAIN")

    with pytest.raises(NetworkChangeError, match="berserial"):
        service(db).apply(device.id, update(), dry_run=True)


def test_a_change_that_changes_nothing_is_refused(db, network_enabled, monkeypatch, fake_clients):
    device = make_device(db)
    report_network(monkeypatch, fake_clients, ip="10.100.1.14", netmask="255.255.255.0")

    with pytest.raises(NetworkChangeError, match="Tidak ada yang berubah"):
        service(db).apply(device.id, update(ip="10.100.1.14"), dry_run=True)
