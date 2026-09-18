"""Device client abstraction.

This module is the *only* place in the application that talks to the
``zkaccess-c3`` library. Routes and (most) services depend on the
:class:`DeviceClient` protocol instead, so tests can inject
:class:`~tests.fakes.FakeDeviceClient` without any physical hardware.

Design notes
------------
* Every network call goes through :meth:`ZKTecoDeviceClient._call`, which adds
  timeouts, bounded retries with exponential backoff and a reconnect attempt.
  Hospital networks drop packets; a single failed socket must not abort a
  multi-device sync.
* All library-specific and firmware-specific workarounds live in
  :mod:`app.services.c3_compat`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.services import c3_compat
from app.services.c3_compat import PanelRefusedError

logger = logging.getLogger(__name__)

#: Device parameter names, as accepted by the panel's GETPARAM command.
#:
#: The network four are read so the panel's own idea of its address can be shown
#: next to the one this database holds. Those two drift apart — a panel moved or
#: re-addressed at the keypad still looks reachable in an old record — and nothing
#: else in the system would reveal it.
DEVICE_PARAMS = [
    "~SerialNumber",
    "~DeviceName",
    "FirmVer",
    "LockCount",
    "ReaderCount",
    "~MaxUserCount",
    "~MaxUserFingerCount",
    "IPAddress",
    "NetMask",
    "GATEWAY",
    "MAC",
]

#: Field names of the `user` table (see `~DeviceName`/GETDATATABLE_CFG output).
USER_TABLE = "user"
USER_FIELDS = ["UID", "CardNo", "Pin", "Password", "Group", "StartTime", "EndTime", "Name"]
USER_COUNT_FIELD = "UID"

TRANSACTION_TABLE = "transaction"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class DeviceError(Exception):
    """Base class for all device communication failures."""


class DeviceUnreachable(DeviceError):
    """The panel could not be reached (network, firewall or wrong IP)."""


class DevicePasswordRequired(DeviceError):
    """The panel demands a connection password."""


class DeviceProtocolError(DeviceError):
    """The panel answered, but with something we could not interpret."""


class DeviceWriteUnsupported(DeviceError):
    """Raised when a write is attempted on a read-only client.

    ``zkaccess-c3`` 0.0.15 only implements the read half of the Pull SDK
    protocol. ``consts.Command`` contains ``GETPARAM``, ``GETDATA``,
    ``DATATABLE_CFG``, ``RTLOG_*``, ``CONTROL`` and ``DATETIME`` but **no**
    write/``SETDATA`` command, so users cannot be pushed to a panel with this
    library. See ``docs/DEVICE_PROTOCOL_NOTES.md`` for the options.
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass
class DeviceInfo:
    serial_number: str | None = None
    firmware_version: str | None = None
    device_name: str | None = None
    model: str | None = None
    mac_address: str | None = None
    lock_count: int | None = None
    reader_count: int | None = None
    max_user_count: int | None = None
    max_fingerprint_count: int | None = None
    personnel_count: int | None = None
    door_status: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceEvent:
    """Normalised realtime event."""

    record_type: str = "event"
    event_time: datetime | None = None
    event_code: int | None = None
    event_type: str | None = None
    card_number: str | None = None
    pin: str | None = None
    door_number: int | None = None
    verification_mode: str | None = None
    in_out_status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthResult:
    online: bool
    latency_ms: float | None = None
    error: str | None = None


def guess_model(lock_count: Any) -> str | None:
    """Derive the panel model from its number of supported locks."""
    try:
        locks = int(lock_count)
    except (TypeError, ValueError):
        return None
    return {1: "C3-100", 2: "C3-200", 4: "C3-300 / C3-400"}.get(locks, f"Unknown ({locks} locks)")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
@runtime_checkable
class DeviceClient(Protocol):
    """Everything the application needs from an access control panel."""

    ip: str
    port: int

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def health_check(self) -> HealthResult: ...
    def get_info(self, *, include_personnel_count: bool = True) -> DeviceInfo: ...
    def count_personnel(self) -> int: ...
    def list_personnel(self, fields: list[str] | None = None) -> list[dict]: ...
    def read_table(self, table: str, fields: list[str] | None = None) -> list[dict]: ...
    def add_or_update_user(self, user: dict) -> None: ...
    def delete_user(self, employee_id: str) -> None: ...
    def open_door(self, door_number: int = 1, duration_seconds: int = 5) -> None: ...
    def cancel_alarm(self) -> None: ...
    def restart(self) -> None: ...
    def set_normal_open(self, door_number: int = 1, enable: bool = True) -> None: ...
    def door_status(self, door_number: int = 1) -> str: ...
    def poll_events(self) -> list[DeviceEvent]: ...
    def set_time(self, when: datetime | None = None) -> None: ...


# ---------------------------------------------------------------------------
# Real implementation
# ---------------------------------------------------------------------------
class ZKTecoDeviceClient:
    """Concrete :class:`DeviceClient` backed by the ``zkaccess-c3`` library."""

    def __init__(
        self,
        ip: str,
        port: int = 4370,
        password: str | None = None,
        *,
        connect_timeout: float = 4.0,
        receive_timeout: float = 4.0,
        receive_retries: int = 2,
        max_retries: int = 3,
        backoff: float = 0.5,
    ) -> None:
        self.ip = ip
        self.port = port
        self.password = password or None
        self.connect_timeout = connect_timeout
        self.receive_timeout = receive_timeout
        self.receive_retries = receive_retries
        self.max_retries = max(1, max_retries)
        self.backoff = backoff
        self._panel = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def panel(self):
        if self._panel is None:
            from c3 import C3  # imported lazily so tests need no hardware lib

            c3_compat.apply_patches()
            self._panel = C3(self.ip, self.port)
            self._panel.receive_timeout = self.receive_timeout
            self._panel.receive_retries = self.receive_retries
        return self._panel

    def connect(self) -> None:
        def _connect() -> None:
            panel = self.panel
            try:
                if self.password:
                    connected = panel.connect(self.password)
                else:
                    connected = panel.connect()
            except ConnectionError as exc:
                raise DeviceUnreachable(
                    f"Tidak bisa connect ke {self.ip}:{self.port} ({exc})"
                ) from exc
            except OSError as exc:
                # Includes socket.timeout and refused connections.
                raise DeviceUnreachable(
                    f"Tidak bisa connect ke {self.ip}:{self.port} ({exc})"
                ) from exc
            if not connected:
                raise DeviceUnreachable(
                    f"Panel {self.ip}:{self.port} menolak koneksi. "
                    "Cek IP/port, firewall, atau password device."
                )

        self._call(_connect, retries=1)

    def disconnect(self) -> None:
        if self._panel is not None:
            try:
                self._panel.disconnect()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("disconnect %s failed: %s", self.ip, exc)
            finally:
                self._panel = None

    def __enter__(self) -> ZKTecoDeviceClient:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.disconnect()

    # -- retry helper ------------------------------------------------------
    def _call(self, fn: Callable[[], Any], *, retries: int | None = None) -> Any:
        """Run ``fn`` with bounded retries and exponential backoff."""
        attempts = retries if retries is not None else self.max_retries
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except (DeviceUnreachable, ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                logger.warning(
                    "device %s attempt %d/%d failed: %s", self.ip, attempt, attempts, exc
                )
                if attempt < attempts:
                    time.sleep(self.backoff * (2 ** (attempt - 1)))
                    self.disconnect()  # force a fresh socket on the next attempt
                    try:
                        self.connect()
                    except DeviceError as reconnect_exc:
                        last_exc = reconnect_exc
            except ValueError as exc:
                # Protocol/parse problems are not transient: do not retry.
                raise DeviceProtocolError(str(exc)) from exc

        raise last_exc if last_exc else DeviceError("unknown device error")

    # -- information -------------------------------------------------------
    def health_check(self) -> HealthResult:
        started = time.perf_counter()
        try:
            self.connect()
            self.panel.get_device_param(["~SerialNumber"])
            return HealthResult(online=True, latency_ms=(time.perf_counter() - started) * 1000)
        except Exception as exc:
            return HealthResult(online=False, error=str(exc))
        finally:
            self.disconnect()

    def get_info(self, *, include_personnel_count: bool = True) -> DeviceInfo:
        info = DeviceInfo()

        def _read() -> None:
            params = self.panel.get_device_param(DEVICE_PARAMS)
            info.raw = dict(params)
            info.serial_number = params.get("~SerialNumber") or None
            info.firmware_version = params.get("FirmVer") or None
            info.device_name = params.get("~DeviceName") or None
            # Prefer the parameter we just read; the library's own `.mac` property
            # has come back empty on this firmware.
            info.mac_address = _clean(params.get("MAC")) or _clean(self.panel.mac)
            info.lock_count = _to_int(params.get("LockCount"))
            info.reader_count = _to_int(params.get("ReaderCount"))
            info.max_user_count = _to_int(params.get("~MaxUserCount"))
            info.max_fingerprint_count = _to_int(params.get("~MaxUserFingerCount"))
            info.model = guess_model(info.lock_count)

            for door in range(1, (info.lock_count or 1) + 1):
                try:
                    info.door_status[str(door)] = str(self.panel.lock_status(door))
                except Exception as exc:  # pragma: no cover - panel specific
                    info.door_status[str(door)] = f"unknown ({exc})"

        self._call(_read)

        if include_personnel_count:
            try:
                info.personnel_count = self.count_personnel()
            except Exception as exc:
                logger.warning("personnel count failed for %s: %s", self.ip, exc)
                info.personnel_count = None

        return info

    def count_personnel(self) -> int:
        """Actual number of users stored on the panel.

        Reads only the ``UID`` field of the ``user`` table. On the firmware
        builds we tested, requesting several fields at once makes the panel
        answer with a non-data payload (see :mod:`app.services.c3_compat`).
        """

        def _read() -> int:
            return c3_compat.count_records_robust(self.panel, USER_TABLE, USER_COUNT_FIELD)

        return self._call(_read)

    def list_personnel(self, fields: list[str] | None = None) -> list[dict]:
        return self.read_table(USER_TABLE, fields or USER_FIELDS)

    def read_table(self, table: str, fields: list[str] | None = None) -> list[dict]:
        def _read() -> list[dict]:
            return c3_compat.read_table_robust(self.panel, table, fields)

        return self._call(_read)

    # -- writing -----------------------------------------------------------
    def add_or_update_user(self, user: dict) -> None:
        """Push a user record to the device.

        NOT IMPLEMENTED *here*: the `zkaccess-c3` library does not expose a write
        command (its command set jumps from ``DATATABLE_CFG = 0x06`` to
        ``GETDATA = 0x08``, omitting ``0x07`` SETDATA), so the Pull SDK command
        set used by this client is read-only.

        Writing is **not** impossible, it simply uses another transport: the
        official 32-bit ``plcommpro.dll`` runs in the Windows push agent and the
        backend calls it over HTTP (``app/services/push_agent.py`` /
        ``app/services/panel_push.py``). See ``AGENTS.md`` before concluding that
        a panel cannot be written to.
        """
        raise DeviceWriteUnsupported(
            "Library zkaccess-c3 tidak punya perintah tulis (SETDATA). "
            "Push user ke device lewat Windows push agent, bukan lewat client ini."
        )

    def delete_user(self, employee_id: str) -> None:
        """Remove a user from the device. NOT IMPLEMENTED (see above)."""
        raise DeviceWriteUnsupported(
            "Library zkaccess-c3 tidak punya perintah hapus user (DELUSER)."
        )

    # -- control -----------------------------------------------------------
    def open_door(self, door_number: int = 1, duration_seconds: int = 5) -> None:
        def _open() -> None:
            from c3 import consts, controldevice

            self.panel.control_device(
                controldevice.ControlDeviceOutput(
                    output_number=door_number,
                    address=consts.ControlOutputAddress.DOOR_OUTPUT,
                    duration=duration_seconds,
                )
            )

        self._call(_open)

    def cancel_alarm(self) -> None:
        def _cancel() -> None:
            from c3 import controldevice

            self.panel.control_device(controldevice.ControlDeviceCancelAlarms())

        self._call(_cancel)

    def restart(self) -> None:
        def _restart() -> None:
            from c3 import controldevice

            self.panel.control_device(controldevice.ControlDeviceRestart())

        self._call(_restart)

    def set_normal_open(self, door_number: int = 1, enable: bool = True) -> None:
        def _set() -> None:
            from c3 import controldevice

            self.panel.control_device(
                controldevice.ControlDeviceNormalOpenStateEnable(
                    door_number=door_number, enable=enable
                )
            )

        self._call(_set)

    def door_status(self, door_number: int = 1) -> str:
        def _read() -> str:
            return str(self.panel.lock_status(door_number))

        return self._call(_read)

    def set_time(self, when: datetime | None = None) -> None:
        def _set() -> None:
            self.panel.set_device_datetime(when)

        self._call(_set)

    # -- realtime ----------------------------------------------------------
    def poll_events(self) -> list[DeviceEvent]:
        """Drain the panel's realtime event buffer (non-blocking)."""

        def _read() -> list[DeviceEvent]:
            return [normalise_event(rec) for rec in self.panel.get_rt_log()]

        try:
            return self._call(_read, retries=1)
        except (DeviceError, PanelRefusedError) as exc:
            logger.debug("realtime poll on %s failed: %s", self.ip, exc)
            raise

    # -- discovery ---------------------------------------------------------
    @staticmethod
    def discover(interface_address: str | None = None, timeout: int = 3) -> list[dict]:
        """Broadcast-discover C3 panels on the local network segment."""
        try:
            from c3 import C3
        except ImportError:  # pragma: no cover
            return []
        c3_compat.apply_patches()
        found = C3.discover(interface_address, timeout)
        return [
            {
                "ip": getattr(dev, "host", None),
                "port": getattr(dev, "port", 4370),
                "serial_number": _clean(getattr(dev, "serial_number", None)),
                "device_name": _clean(getattr(dev, "device_name", None)),
            }
            for dev in found
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clean(value: Any) -> str | None:
    """The library returns a literal '?' for unavailable string values."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in {"", "?"} else text


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalise_event(record: Any) -> DeviceEvent:
    """Convert a library record into a :class:`DeviceEvent`."""
    raw: dict[str, Any] = {}
    for attr in (
        "card_no",
        "pin",
        "verified",
        "port_nr",
        "event_type",
        "in_out_state",
        "time_second",
    ):
        if hasattr(record, attr):
            raw[attr] = str(getattr(record, attr))

    if (
        getattr(record, "is_event", lambda: False)()
        and not getattr(record, "is_door_alarm", lambda: False)()
    ):
        record_type = "event"
    elif getattr(record, "is_door_alarm", lambda: False)():
        record_type = "door_alarm_status"
    else:
        record_type = type(record).__name__

    when = getattr(record, "time_second", None)
    if not isinstance(when, datetime):
        when = None

    return DeviceEvent(
        record_type=record_type,
        event_time=when,
        event_code=_enum_value(getattr(record, "event_type", None), default_key="event_type"),
        event_type=_enum_name(getattr(record, "event_type", None)),
        card_number=_str_or_none(getattr(record, "card_no", None)),
        pin=_str_or_none(getattr(record, "pin", None)),
        door_number=getattr(record, "port_nr", None),
        verification_mode=_enum_name(getattr(record, "verified", None)),
        in_out_status=_enum_name(getattr(record, "in_out_state", None)),
        raw=raw,
    )


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _enum_value(value: Any, default_key: str = "value") -> int | None:
    if value is None:
        return None
    raw = getattr(value, default_key, None)
    return raw if isinstance(raw, int) else None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return None if text in {"", "0"} else text


# ---------------------------------------------------------------------------
# Factory (dependency-injection seam for tests)
# ---------------------------------------------------------------------------
ClientFactory = Callable[..., DeviceClient]

_default_factory: ClientFactory = ZKTecoDeviceClient
_current_factory: ClientFactory = _default_factory


def set_client_factory(factory: ClientFactory | None) -> None:
    """Override the factory used by :func:`build_client` (tests only)."""
    global _current_factory
    _current_factory = factory or _default_factory


def build_client(
    ip: str,
    port: int = 4370,
    password: str | None = None,
    **kwargs: Any,
) -> DeviceClient:
    """Create a device client using the currently configured factory."""
    return _current_factory(ip, port, password, **kwargs)
