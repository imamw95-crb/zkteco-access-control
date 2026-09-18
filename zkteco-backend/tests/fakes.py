"""In-memory device doubles used by the tests.

No physical panel and no `zkaccess-c3` installation is required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.device_client import (
    DeviceEvent,
    DeviceInfo,
    DeviceUnreachable,
    DeviceWriteUnsupported,
    HealthResult,
    guess_model,
)

#: Field layout of the `transaction` table, as reported by a real C3-100.
TRANSACTION_FIELDS = [
    "Pin",
    "Verified",
    "DoorID",
    "EventType",
    "InOutState",
    "Time_second",
    "Index",
]


class FakeDeviceClient:
    """A scriptable stand-in for :class:`ZKTecoDeviceClient`."""

    #: class level registry so tests can assert on created clients
    created: list[FakeDeviceClient] = []

    #: IP -> behaviour overrides
    online: dict[str, bool] = {}
    users: dict[str, list[dict]] = {}
    transactions: dict[str, list[dict]] = {}
    fail_after_connect: dict[str, str] = {}
    unreachable_errors: dict[str, str] = {}

    def __init__(self, ip: str, port: int = 4370, password: str | None = None, **kwargs):
        self.ip = ip
        self.port = port
        self.password = password or None
        self.options = kwargs
        self._connected = False
        self._door_open = False
        self.history: list[str] = []
        FakeDeviceClient.created.append(self)

    # -- test control ------------------------------------------------------
    @classmethod
    def reset(cls) -> None:
        cls.created.clear()
        cls.online.clear()
        cls.users.clear()
        cls.transactions.clear()
        cls.fail_after_connect.clear()
        cls.unreachable_errors.clear()

    # -- DeviceClient protocol --------------------------------------------
    def connect(self) -> None:
        if not FakeDeviceClient.online.get(self.ip, True):
            raise DeviceUnreachable(
                FakeDeviceClient.unreachable_errors.get(
                    self.ip, f"Tidak bisa connect ke {self.ip}:{self.port}"
                )
            )
        if self.password and self.password != "secret":
            raise DeviceUnreachable("password salah")
        self._connected = True
        self.history.append("connect")

    def disconnect(self) -> None:
        self._connected = False
        self.history.append("disconnect")

    def __enter__(self) -> FakeDeviceClient:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.disconnect()

    def health_check(self) -> HealthResult:
        if not FakeDeviceClient.online.get(self.ip, True):
            return HealthResult(
                online=False,
                error=FakeDeviceClient.unreachable_errors.get(self.ip, "offline"),
            )
        return HealthResult(online=True, latency_ms=12.5)

    def get_info(self, *, include_personnel_count: bool = True) -> DeviceInfo:
        self._ensure_connected()
        if self.ip in FakeDeviceClient.fail_after_connect:
            raise RuntimeError(FakeDeviceClient.fail_after_connect[self.ip])

        shift = int(self.ip.split(".")[-1])
        locks = 2 if shift >= 20 else 1
        info = DeviceInfo(
            serial_number=f"TEST{shift:08d}",
            firmware_version="AC Ver 5.4.3.2001 Sep 25 2019",
            device_name="C3-100",
            model=guess_model(locks),
            mac_address=f"00:11:22:33:44:{shift:02X}",
            lock_count=locks,
            reader_count=2,
            max_user_count=300,
            max_fingerprint_count=10,
            raw={"~SerialNumber": f"TEST{shift:08d}", "LockCount": str(locks)},
        )
        info.door_status = {str(door): self.door_status(door) for door in range(1, locks + 1)}
        if include_personnel_count:
            info.personnel_count = self.count_personnel()
        return info

    def count_personnel(self) -> int:
        self._ensure_connected()
        return len(FakeDeviceClient.users.get(self.ip, []))

    def list_personnel(self, fields: list[str] | None = None) -> list[dict]:
        return self.read_table("user", fields)

    def read_table(self, table: str, fields: list[str] | None = None) -> list[dict]:
        self._ensure_connected()
        if table == "user":
            rows = FakeDeviceClient.users.get(self.ip, [])
        elif table == "transaction":
            rows = FakeDeviceClient.transactions.get(self.ip, [])
            if FakeDeviceClient.transactions.get(self.ip, None) is None:
                raise RuntimeError("Wrong table returned by panel. Expected 5, received 0")
            fields = fields or TRANSACTION_FIELDS
        else:
            rows = []

        if fields:
            return [{k: row.get(k) for k in fields} for row in rows]
        return [dict(row) for row in rows]

    def add_or_update_user(self, user: dict) -> None:
        raise DeviceWriteUnsupported("read-only library")

    def delete_user(self, employee_id: str) -> None:
        raise DeviceWriteUnsupported("read-only library")

    def open_door(self, door_number: int = 1, duration_seconds: int = 5) -> None:
        self._ensure_connected()
        self._door_open = True
        self.history.append(f"open_door:{door_number}:{duration_seconds}")

    def cancel_alarm(self) -> None:
        self._ensure_connected()
        self.history.append("cancel_alarm")

    def restart(self) -> None:
        self._ensure_connected()
        self.history.append("restart")

    def set_normal_open(self, door_number: int = 1, enable: bool = True) -> None:
        self._ensure_connected()
        self.history.append(f"normal_open:{door_number}:{enable}")

    def door_status(self, door_number: int = 1) -> str:
        self._ensure_connected()
        return "OPEN" if self._door_open else "CLOSED"

    def poll_events(self) -> list[DeviceEvent]:
        self._ensure_connected()
        return [
            DeviceEvent(
                record_type="event",
                event_time=datetime(2026, 9, 16, 8, 30, tzinfo=timezone.utc),
                event_code=0,
                event_type="NORMAL_PUNCH_OPEN",
                card_number="12345",
                pin="1001",
                door_number=1,
                verification_mode="CARD",
                in_out_status="IN",
                raw={"pin": "1001"},
            )
        ]

    def set_time(self, when: datetime | None = None) -> None:
        self._ensure_connected()

    @staticmethod
    def discover(interface_address: str | None = None, timeout: int = 3) -> list[dict]:
        return [
            {
                "ip": "10.100.1.90",
                "port": 4370,
                "serial_number": "NEW00001",
                "device_name": "C3-100",
            },
            {
                "ip": "10.100.1.91",
                "port": 4370,
                "serial_number": "NEW00002",
                "device_name": "C3-200",
            },
        ]

    # -- internal ----------------------------------------------------------
    def _ensure_connected(self) -> None:
        if not self._connected:
            raise DeviceUnreachable(f"not connected to {self.ip}")


def install_fake_client(*, online: list[str] | None = None) -> None:
    """Point the app's client factory at :class:`FakeDeviceClient`."""
    from app.services.device_client import set_client_factory

    FakeDeviceClient.reset()
    for ip in online or []:
        FakeDeviceClient.online[ip] = True
    set_client_factory(FakeDeviceClient)


def uninstall_fake_client() -> None:
    from app.services.device_client import set_client_factory

    set_client_factory(None)
    FakeDeviceClient.reset()


class OpenSocket:
    """Stands in for the socket a successful ``create_connection`` returns."""

    def __enter__(self) -> OpenSocket:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def fake_connect(open_ips: set[str]):
    """A ``socket.create_connection`` that only succeeds for the given hosts.

    Lets the network scan be tested without a network: the hosts not listed behave
    like a refused port, which is what an unused address on the panel subnet does.
    """

    def connect(address, timeout=None, source_address=None):
        host = address[0]
        if host not in open_ips:
            raise ConnectionRefusedError(f"refused {host}")
        return OpenSocket()

    return connect
