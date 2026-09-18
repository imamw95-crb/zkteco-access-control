"""Network scan: range parsing, guards, TCP probing and identification.

No physical panel and no network: the TCP probe is replaced by a double, and
identification goes through ``FakeDeviceClient`` (installed by the autouse
``fake_clients`` fixture in ``tests/conftest.py``).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models import Device
from app.schemas import NetworkScanRequest
from app.services import network_scan
from app.services.network_scan import NetworkScanService, ScanRangeError, parse_hosts
from tests.fakes import fake_connect


def scan_request(**overrides) -> NetworkScanRequest:
    payload = {"ranges": ["10.100.1.0/24"], "identify": False}
    payload.update(overrides)
    return NetworkScanRequest(**payload)


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------
def test_parse_hosts_expands_a_slash_24_to_its_usable_hosts():
    hosts = parse_hosts(["10.100.1.0/24"])

    assert len(hosts) == 254  # network and broadcast address excluded
    assert hosts[0] == "10.100.1.1"
    assert hosts[-1] == "10.100.1.254"


def test_parse_hosts_accepts_single_ips_ranges_and_commas():
    hosts = parse_hosts(["10.100.1.12", "10.100.1.10-10.100.1.11,10.100.1.12"])

    assert hosts == ["10.100.1.12", "10.100.1.10", "10.100.1.11"]  # duplicate dropped


@pytest.mark.parametrize("bad", ["10.100.1.0/33", "10.100.1.300", "bukan-ip", ""])
def test_parse_hosts_rejects_nonsense(bad):
    with pytest.raises(ScanRangeError):
        parse_hosts([bad])


def test_parse_hosts_refuses_a_huge_range_before_expanding_it():
    """10.0.0.0/8 is 16 million addresses — the cap must fire without expanding.

    Materialising that list first would exhaust memory long before the guard could
    report anything, so the size is measured by arithmetic.
    """
    with pytest.raises(ScanRangeError) as excinfo:
        parse_hosts(["10.0.0.0/8"])

    assert "terlalu banyak" in str(excinfo.value)


def test_parse_hosts_refuses_more_than_the_configured_cap(monkeypatch):
    monkeypatch.setattr(settings, "scan_max_hosts", 4)

    with pytest.raises(ScanRangeError):
        parse_hosts(["10.100.1.0/29"])


def test_parse_hosts_rejects_a_subnet_outside_the_allowed_list():
    """A scan is visible on the network, so only agreed subnets may be swept."""
    with pytest.raises(ScanRangeError) as excinfo:
        parse_hosts(["192.168.9.0/30"])

    message = str(excinfo.value)
    assert "diizinkan" in message
    assert settings.scan_allowed_networks in message


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
def test_probe_host_reports_open_and_refused(monkeypatch):
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))

    hit = network_scan.probe_host("10.100.1.5", 4370, 1.0)
    assert hit.open is True
    assert hit.latency_ms is not None
    assert hit.error is None

    miss = network_scan.probe_host("10.100.1.6", 4370, 1.0)
    assert miss.open is False
    assert "refused" in miss.error


def test_scan_lists_only_the_hosts_that_answered(monkeypatch):
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))

    result = NetworkScanService(None).scan(scan_request(ranges=["10.100.1.4/30"]))

    assert result["hosts_scanned"] == 2  # .5 and .6
    assert result["hosts_open"] == 1
    assert [host["ip"] for host in result["hosts"]] == ["10.100.1.5"]


def test_scan_streams_progress_and_finishes_with_a_summary(monkeypatch):
    """A /24 takes seconds, so the caller has to be able to see movement."""
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect(set()))

    events = list(NetworkScanService(None).iter_scan(scan_request(ranges=["10.100.1.0/28"])))

    assert events[-1]["type"] == "summary"
    assert events[-1]["hosts_scanned"] == 14
    assert events[-1]["hosts_open"] == 0
    progress = [event for event in events if event["type"] == "progress"]
    assert progress[-1]["done"] == 14
    assert progress[-1]["total"] == 14


def test_local_networks_admits_when_there_is_no_route(monkeypatch):
    monkeypatch.setattr(
        network_scan,
        "local_address_for",
        lambda target: "192.168.200.230" if target.startswith("10.") else None,
    )

    routes = {item["range"]: item["local_address"] for item in NetworkScanService.local_networks()}

    assert routes["10.100.1.0/24"] == "192.168.200.230"
    # No route: a scan of that subnet comes back empty, and that is not a dead panel.
    assert routes["192.168.1.0/24"] is None


# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------
def test_identify_reads_the_identity_through_the_device_client(monkeypatch, fake_clients):
    fake_clients.reset()
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))

    result = NetworkScanService(None).scan(scan_request(ranges=["10.100.1.4/30"], identify=True))

    host = result["hosts"][0]
    assert host["serial_number"] == "TEST00000005"
    assert host["firmware_version"] == "AC Ver 5.4.3.2001 Sep 25 2019"
    assert host["lock_count"] == 1
    assert host["error"] is None


def test_scan_never_writes_to_a_panel(monkeypatch, fake_clients):
    """Identification is a read: no user write, no access grant, no door control."""
    fake_clients.reset()
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))

    NetworkScanService(None).scan(scan_request(ranges=["10.100.1.4/30"], identify=True))

    assert [client.ip for client in fake_clients.created] == ["10.100.1.5"]
    for client in fake_clients.created:
        # `open_door`, `add_or_update_user` and friends all append to `history`.
        assert client.history == ["connect", "disconnect"]


def test_a_panel_that_refuses_the_read_keeps_its_open_port(monkeypatch, fake_clients):
    """A failed read is not a closed port — -2/-107/-112 happen on live panels."""
    fake_clients.reset()
    fake_clients.online["10.100.1.5"] = False
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))

    result = NetworkScanService(None).scan(scan_request(ranges=["10.100.1.4/30"], identify=True))

    host = result["hosts"][0]
    assert host["ip"] == "10.100.1.5"
    assert host["open"] is True
    assert "identitas tidak terbaca" in host["error"]
    assert host["serial_number"] is None


def test_one_unreadable_host_does_not_stop_the_sweep(monkeypatch, fake_clients):
    fake_clients.reset()
    fake_clients.online["10.100.1.5"] = False  # 10.100.1.6 still answers
    monkeypatch.setattr(
        network_scan.socket, "create_connection", fake_connect({"10.100.1.5", "10.100.1.6"})
    )

    result = NetworkScanService(None).scan(scan_request(ranges=["10.100.1.4/30"], identify=True))

    assert [host["ip"] for host in result["hosts"]] == ["10.100.1.5", "10.100.1.6"]
    assert result["hosts"][1]["serial_number"] == "TEST00000006"


def test_a_panel_that_moved_is_matched_by_serial_not_by_ip(monkeypatch, fake_clients, db):
    """The IP in the database can be stale; the serial number cannot.

    A panel re-addressed at the keypad still answers with its own serial, and that
    is the only way to recognise it — otherwise it looks like a brand new device
    while the old record keeps pointing at an address nobody uses.
    """
    fake_clients.reset()
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))
    device = Device(
        name="RUANGAN SERVER", ip="10.100.1.99", port=4370, serial_number="TEST00000005"
    )
    db.add(device)
    db.commit()

    result = NetworkScanService(db).scan(scan_request(ranges=["10.100.1.4/30"], identify=True))

    host = result["hosts"][0]
    assert host["registered"] is True
    assert host["device_id"] == device.id
    # The stale address is reported too, so the UI can offer to correct the record
    # instead of leaving the operator to notice the mismatch themselves.
    assert host["device_ip"] == "10.100.1.99"
    assert host["device_ip"] != host["ip"]


def test_the_panels_own_network_config_is_reported(monkeypatch, fake_clients):
    """Read-only: where the panel thinks it lives, so a drift is visible at all.

    The panel can answer on one address while its own configuration says another —
    and the database may hold a third. Only this read makes that comparable.
    """
    from app.services.device_client import DeviceInfo

    fake_clients.reset()
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))

    def fake_get_info(self, *, include_personnel_count=True):
        return DeviceInfo(
            serial_number="TEST00000005",
            raw={"IPAddress": "10.100.1.5", "NetMask": "255.255.255.0", "GATEWAY": "?"},
        )

    monkeypatch.setattr(fake_clients, "get_info", fake_get_info)

    result = NetworkScanService(None).scan(scan_request(ranges=["10.100.1.4/30"], identify=True))

    host = result["hosts"][0]
    assert host["panel_ip"] == "10.100.1.5"
    assert host["panel_netmask"] == "255.255.255.0"
    # The firmware answers a literal '?' for a parameter it cannot report.
    assert host["panel_gateway"] is None


def test_a_database_problem_does_not_throw_away_the_findings(monkeypatch, fake_clients):
    """The sweep is the slow part; a database hiccup must not discard what it found."""
    fake_clients.reset()
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))

    class BrokenSession:
        def __getattr__(self, name):
            raise RuntimeError("database tidak bisa dihubungi")

    result = NetworkScanService(BrokenSession()).scan(
        scan_request(ranges=["10.100.1.4/30"], identify=True)
    )

    assert result["hosts_open"] == 1
    assert result["hosts"][0]["serial_number"] == "TEST00000005"
    assert result["hosts"][0]["registered"] is False  # unknown, not "definitely new"
