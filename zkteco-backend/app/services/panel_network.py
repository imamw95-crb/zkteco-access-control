"""Changing a panel's *own* network configuration — the "Modify IP Address" button.

ZKAccess 3.5 lets an operator re-address a panel from the software. This is that
feature, and it is the most dangerous thing in this codebase, because it is the only
operation that can leave a panel **unreachable with no way back**: everything else can
be retried or corrected from here, but a panel given a wrong address has to be put
right at the device — in the ceiling of a ward, if that is where it is mounted.

So it ships **switched off** (`PANEL_NETWORK_WRITE_ENABLED`, default false) and, once
switched on, still refuses to write without a dry run of the exact same values.

Two measured facts shape the guards:

* This firmware does **not** report `GATEWAY` — the `c3` library drops the key and the
  agent's official-SDK read answers an empty string (measured on a live panel). The
  gateway therefore cannot be copied from the panel and has to come from the operator.
* Whether `SetDeviceParam` clears parameters it was *not* given — the way
  `SetDeviceData` clears omitted record fields — is **not known**. So every parameter
  this software can read is sent in one call, and nothing else is ever touched: the
  MAC is carried over, never edited.

Flow: read the panel (which also proves we are talking to the device we think we are)
→ validate → dry run → write → verify against the **new** address → only then store
the new address against the device record.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass, field

from app.config import settings
from app.services.device_client import DeviceError, build_client
from app.services.network_scan import allowed_networks
from app.services.push_agent import PushAgentClient

logger = logging.getLogger(__name__)

#: The parameters that make up a panel's network configuration. All of them are sent
#: on every write: a partially-specified write could clear something silently.
NETWORK_PARAMS = ("IPAddress", "NetMask", "GATEWAY", "MAC")

#: After a write the panel drops the old connection. It needs a moment before the new
#: address answers, and a single failed attempt proves nothing (panels also refuse
#: concurrent connections).
VERIFY_ATTEMPTS = 4
VERIFY_DELAY_SECONDS = 1.5

DISABLED_MESSAGE = (
    "Mengubah alamat jaringan panel sedang tidak diaktifkan. Fitur ini bisa membuat "
    "panel tidak terjangkau lagi dan hanya bisa diperbaiki fisik di lokasinya, jadi "
    "harus dinyalakan sadar oleh operator: set PANEL_NETWORK_WRITE_ENABLED=true di "
    "environment server, dan uji di SATU panel lebih dulu. Selama itu, pakai "
    '"Perbaiki IP di dashboard" (hanya memperbaiki catatan di database).'
)


class PanelNetworkError(RuntimeError):
    """The change could not be prepared, written or verified."""


class PanelNetworkWriteDisabled(PanelNetworkError):
    """Writing a panel's network configuration is switched off."""


class NetworkChangeError(ValueError):
    """The requested values are not acceptable (bad address, netmask, or duplicate)."""


def _text(value: object) -> str | None:
    """The firmware answers a literal ``?`` for a parameter it cannot report."""
    text = "" if value is None else str(value).strip()
    return None if text in {"", "?"} else text


@dataclass
class PanelNetworkState:
    """What the panel itself says its network configuration is (read-only)."""

    device_id: int
    name: str
    ip: str
    port: int
    serial_number: str | None = None
    ip_address: str | None = None
    netmask: str | None = None
    gateway: str | None = None
    mac: str | None = None
    #: Whether a write would be accepted at all (PANEL_NETWORK_WRITE_ENABLED).
    #: Reported with the read so the operator learns it *before* pressing the button.
    write_enabled: bool = False


@dataclass
class PanelNetworkChange:
    """A validated plan, and — when it was not a dry run — what happened."""

    device_id: int
    name: str
    current: PanelNetworkState
    new_ip: str
    netmask: str
    gateway: str
    enabled: bool = False
    dry_run: bool = True
    written: bool = False
    reachable_at_new_address: bool | None = None
    record_updated: bool = False
    warnings: list[str] = field(default_factory=list)
    #: Exactly what will be (or was) handed to the panel, for display and for tests.
    params: dict[str, str] = field(default_factory=dict)


class PanelNetworkService:
    """Reads and (when explicitly enabled) writes a panel's network configuration."""

    def __init__(self, db, *, agent_factory: type[PushAgentClient] = PushAgentClient):
        self.db = db
        self._agent_factory = agent_factory

    # -- read --------------------------------------------------------------
    def read(self, device_id: int) -> PanelNetworkState:
        """Read the panel's own network configuration (never writes)."""
        from app.services.device_service import DeviceService

        device = DeviceService(self.db).get(device_id)
        state = PanelNetworkState(
            device_id=device.id, name=device.name, ip=device.ip, port=device.port
        )
        try:
            with DeviceService(self.db).client_for(device) as client:
                info = client.get_info(include_personnel_count=False)
        except Exception as exc:  # noqa: BLE001 - reported to the operator as-is
            device.is_online = False
            device.last_error = str(exc)[:2000]
            self.db.commit()
            raise DeviceError(
                f"Panel {device.name} ({device.ip}) tidak bisa dibaca: {exc}"
            ) from exc

        state.serial_number = info.serial_number
        state.ip_address = _text(info.raw.get("IPAddress"))
        state.netmask = _text(info.raw.get("NetMask"))
        state.gateway = _text(info.raw.get("GATEWAY"))
        state.mac = _text(info.raw.get("MAC"))
        state.write_enabled = bool(settings.panel_network_write_enabled)
        return state

    # -- plan --------------------------------------------------------------
    def plan(self, device_id: int, payload) -> PanelNetworkChange:
        """Validate the request against the panel's real state. No I/O to the agent.

        A plan is always allowed, even while writing is switched off: seeing exactly
        what *would* be sent is how an operator decides whether to switch it on.
        """
        from app.services.device_service import DeviceService

        devices = DeviceService(self.db)
        device = devices.get(device_id)
        state = self.read(device_id)

        # We are about to send a panel to a new address. Make sure the panel that
        # answered IS this device before we change anything about it.
        if (
            device.serial_number
            and state.serial_number
            and device.serial_number != state.serial_number
        ):
            raise NetworkChangeError(
                f"Panel yang menjawab di {device.ip} berserial {state.serial_number}, "
                f"sedangkan device ini tercatat berserial {device.serial_number}. "
                "Alamat lama kemungkinan sudah dipakai panel lain — perbaiki dulu lewat "
                "tombol Perbaiki IP di dashboard."
            )

        new_ip = self._valid_ip(payload.ip, "IP baru")
        if not any(ipaddress.ip_address(new_ip) in net for net in allowed_networks()):
            raise NetworkChangeError(
                f"IP baru {new_ip} di luar subnet yang diizinkan "
                f"({settings.scan_allowed_networks}). Kalau panel dipindah ke subnet lain, "
                "tambahkan subnet itu ke SCAN_ALLOWED_NETWORKS lebih dulu supaya panel "
                "baru masih bisa dijangkau dashboard."
            )
        netmask = self._valid_netmask(payload.netmask)
        gateway = (
            ""
            if not (payload.gateway or "").strip()
            else self._valid_ip(payload.gateway, "Gateway")
        )

        clash = devices.get_by_ip(new_ip, device.port)
        if clash is not None and clash.id != device.id:
            raise NetworkChangeError(
                f'IP {new_ip}:{device.port} sudah dipakai device "{clash.name}".'
            )

        params = {
            "IPAddress": new_ip,
            "NetMask": netmask,
            "GATEWAY": gateway,
            # Never edited: it is the panel's identity on the wire, and we only carry
            # the value we just read so the write stays complete.
            "MAC": state.mac or device.mac_address or "",
        }
        if (
            params["IPAddress"] == (state.ip_address or device.ip)
            and netmask == (state.netmask or "")
            and gateway == (state.gateway or "")
        ):
            raise NetworkChangeError("Tidak ada yang berubah: IP, netmask, dan gateway sama.")

        warnings = [
            f"Setelah ditulis, panel {device.name} tidak lagi menjawab di {device.ip}. "
            f"Semua fitur (health, kirim personel, buka pintu) akan memakai {new_ip}.",
            "Kalau alamat barunya salah, panel ini TIDAK bisa diperbaiki dari sini — "
            "harus dibetulkan langsung di panel (keypad/layar di lokasinya).",
        ]
        if not gateway:
            warnings.append(
                "Gateway ditulis KOSONG. Firmware ini tidak melaporkan gateway, jadi "
                "nilainya tidak bisa dibaca dan disalin — kalau panel butuh gateway, "
                "isikan manual sebelum menulis."
            )
        if not settings.panel_network_write_enabled:
            warnings.append(
                "Menulis belum diaktifkan (PANEL_NETWORK_WRITE_ENABLED=false): rencana "
                "ini hanya bisa dilihat, belum bisa dikirim ke panel."
            )

        return PanelNetworkChange(
            device_id=device.id,
            name=device.name,
            current=state,
            new_ip=new_ip,
            netmask=netmask,
            gateway=gateway,
            enabled=settings.panel_network_write_enabled,
            dry_run=True,
            warnings=warnings,
            params=params,
        )

    def apply(self, device_id: int, payload, *, dry_run: bool = True) -> PanelNetworkChange:
        """Plan, and — unless ``dry_run`` — write it and verify the new address."""
        # Checked BEFORE anything else: while writing is off, a write request must not
        # even connect to the panel, so the answer cannot depend on the hardware.
        if not dry_run:
            self._assert_enabled()
        change = self.plan(device_id, payload)
        if dry_run:
            return change

        agent = self._agent_factory()
        # Any failure here (no agent, unreachable panel) lands before the write.
        agent.set_params(change.current.ip, change.params)
        change.dry_run = False
        change.written = True
        logger.warning(
            "panel %s (%s) alamatnya ditulis ke %s",
            change.name,
            change.current.ip,
            change.new_ip,
        )

        change.reachable_at_new_address = self._verify(change.new_ip, change.current.port)
        if change.reachable_at_new_address:
            self._store_new_address(change.device_id, change.new_ip)
            change.record_updated = True
        else:
            change.warnings.append(
                f"Panel TIDAK menjawab di {change.new_ip} setelah {VERIFY_ATTEMPTS} percobaan. "
                f"Perintah tulisnya diterima panel, jadi alamatnya kemungkinan sudah berubah. "
                f"Catatan di dashboard SENGAJA tidak diubah supaya alamat lama masih terlihat. "
                'Cek kabel/switch/VLAN di lokasi panel, lalu pakai "Perbaiki IP di dashboard" '
                "kalau alamat barunya benar."
            )
        return change

    # -- internals ---------------------------------------------------------
    def _assert_enabled(self) -> None:
        if not settings.panel_network_write_enabled:
            raise PanelNetworkWriteDisabled(DISABLED_MESSAGE)

    def _verify(self, ip: str, port: int) -> bool:
        """Dial the NEW address. A single failure proves nothing, so this retries."""
        for attempt in range(VERIFY_ATTEMPTS):
            client = build_client(ip, port)
            try:
                with client:
                    if client.health_check().online:
                        return True
            except Exception as exc:  # noqa: BLE001 - transient -2/-107/-112 included
                logger.info("verifikasi %s percobaan %s gagal: %s", ip, attempt + 1, exc)
            time.sleep(VERIFY_DELAY_SECONDS)
        return False

    def _store_new_address(self, device_id: int, new_ip: str) -> None:
        from app.schemas import DeviceUpdate
        from app.services.device_service import DeviceService

        DeviceService(self.db).update(device_id, DeviceUpdate(ip=new_ip))

    @staticmethod
    def _valid_ip(value: str | None, label: str) -> str:
        try:
            address = ipaddress.IPv4Address((value or "").strip())
        except ValueError as exc:
            raise NetworkChangeError(f"{label} tidak valid: {value!r}") from exc
        return str(address)

    @staticmethod
    def _valid_netmask(value: str | None) -> str:
        text = (value or "").strip()
        try:
            # A netmask is only a netmask if it can be used as a prefix length.
            ipaddress.ip_network(f"0.0.0.0/{text}")
        except ValueError as exc:
            raise NetworkChangeError(f"NetMask tidak valid: {value!r}") from exc
        return text
