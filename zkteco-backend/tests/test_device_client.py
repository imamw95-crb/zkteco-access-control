"""The real panel client, with the DLL-backed library replaced by a stub.

`device_client` is the only place that talks to a panel in-process, and the things
worth pinning here are all about *what it asks the panel for*: the device
parameters, where the panel's own network configuration comes from, and falling
back to the `MAC` parameter because the library's `.mac` property comes back empty
on this firmware.
"""

from __future__ import annotations

from app.services.device_client import DEVICE_PARAMS, ZKTecoDeviceClient

PANEL_PARAMS = {
    "~SerialNumber": "AJYS082162447",
    "~DeviceName": "C3-100",
    "FirmVer": "AC Ver 5.4.3.2001",
    "LockCount": "1",
    "ReaderCount": "1",
    "~MaxUserCount": "3000",
    "~MaxUserFingerCount": "3000",
    "IPAddress": "10.100.1.12",
    "NetMask": "255.255.255.0",
    "GATEWAY": "",
    "MAC": "00:17:61:FF:41:98",
}


class StubPanel:
    """Records what it was asked for, so the request itself can be asserted."""

    def __init__(self, params: dict, *, mac_property: str = "") -> None:
        self.params = params
        self.mac_property = mac_property
        self.asked: list[str] = []

    def get_device_param(self, names: list[str]) -> dict:
        self.asked = list(names)
        return {name: self.params.get(name, "") for name in names}

    @property
    def mac(self) -> str:
        return self.mac_property

    def lock_status(self, _door: int) -> int:
        return 0


def _client(panel: StubPanel) -> ZKTecoDeviceClient:
    client = ZKTecoDeviceClient("10.100.1.12")
    # Injected: the `panel` property would otherwise import the real `c3` library,
    # which needs hardware (and, on this machine, the Windows-only DLL).
    client._panel = panel
    return client


def test_device_info_reads_the_panels_own_network_configuration():
    """The address a panel believes it has is the only way to spot drift.

    The database's `ip` is the address we dial; the panel's `IPAddress` is what it
    thinks it is. Those two drift apart when a panel is re-addressed at the keypad,
    and without reading this parameter nothing in the system would show it — the
    panel simply looks unreachable while the record still looks correct.
    """
    panel = StubPanel(PANEL_PARAMS)

    info = _client(panel).get_info(include_personnel_count=False)

    for name in ("IPAddress", "NetMask", "GATEWAY", "MAC"):
        assert name in panel.asked, f"{name} tidak diminta dari panel"
        assert info.raw[name] == PANEL_PARAMS[name]
    assert info.raw["IPAddress"] == "10.100.1.12"


def test_mac_comes_from_the_parameter_when_the_library_property_is_empty():
    """`panel.mac` has answered empty on this firmware; the parameter does not.

    An empty `mac_address` on every device row was the visible symptom.
    """
    panel = StubPanel(PANEL_PARAMS, mac_property="")

    info = _client(panel).get_info(include_personnel_count=False)

    assert info.mac_address == "00:17:61:FF:41:98"


def test_the_library_property_is_still_used_when_the_parameter_is_missing():
    panel = StubPanel({**PANEL_PARAMS, "MAC": ""}, mac_property="AA:BB:CC:DD:EE:FF")

    info = _client(panel).get_info(include_personnel_count=False)

    assert info.mac_address == "AA:BB:CC:DD:EE:FF"


def test_the_network_parameters_are_requested_alongside_the_rest():
    """GETPARAM takes a list, so four more names cost no extra round trip.

    Worth stating because panels answer one connection at a time: a second request
    per panel is contention, not just latency.
    """
    assert {"IPAddress", "NetMask", "GATEWAY", "MAC"} <= set(DEVICE_PARAMS)
