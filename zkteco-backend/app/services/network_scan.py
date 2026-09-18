"""Finding C3 panels on an IP range (CIDR) — separate from the UDP broadcast.

The broadcast search (`c3.C3.discover()`, used by ``GET /api/devices/discover``) only
reaches panels on the *same* network segment as the backend. Panels on another subnet
that the host merely has a *route* to — e.g. ``10.100.1.0/24`` from a server sitting on
``192.168.0.27`` — are found here instead: a TCP connect to port 4370, then the panel's
identity read through the ``DeviceClient`` seam (GETPARAM).

Nothing in this module writes to a panel. There is no ``user``/``userauthorize`` write,
no door control and no change to the panel's own network parameters — the worst outcome
of a wrong range is a list of hosts that answered nothing.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

from app.config import settings
from app.services.device_client import build_client

logger = logging.getLogger(__name__)

#: How many probed hosts are reported as one progress line. A /24 finishes in a few
#: seconds; reporting every host would only flood the operator's screen.
PROGRESS_CHUNK = 32


class ScanRangeError(ValueError):
    """The range is invalid, outside the allowed subnets, or too large."""


@dataclass
class ScanHit:
    """One host that answered. Closed hosts are counted, never listed."""

    ip: str
    port: int = 4370
    open: bool = False
    latency_ms: float | None = None
    serial_number: str | None = None
    device_name: str | None = None
    firmware_version: str | None = None
    lock_count: int | None = None
    model: str | None = None
    mac_address: str | None = None
    registered: bool = False
    device_id: int | None = None
    #: The address this database holds for that device. A panel answering at a
    #: *different* address means the record is stale — health checks, pushes and door
    #: control would all dial the wrong host, and only a scan reveals it.
    device_ip: str | None = None
    #: The panel's OWN network configuration, as the panel reports it (read-only).
    #: It can differ from the address it answered on and from the stored one.
    panel_ip: str | None = None
    panel_netmask: str | None = None
    panel_gateway: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# IP ranges
# ---------------------------------------------------------------------------
def allowed_networks() -> list[ipaddress.IPv4Network]:
    """Subnets this deployment permits scanning (``SCAN_ALLOWED_NETWORKS``)."""
    networks: list[ipaddress.IPv4Network] = []
    for token in (settings.scan_allowed_networks or "").replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("SCAN_ALLOWED_NETWORKS contains %r, which is not a subnet", token)
    return networks


def _network(token: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(token, strict=False)  # '10.100.1.12' -> /32
    except ValueError as exc:
        raise ScanRangeError(f"Alamat/CIDR tidak valid: {token}") from exc
    if network.version != 4:
        raise ScanRangeError(f"Hanya IPv4 yang didukung: {token}")
    return network


def _range_bounds(token: str) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]:
    start, _, end = token.partition("-")
    try:
        first = ipaddress.IPv4Address(start.strip())
        last = ipaddress.IPv4Address(end.strip())
    except ValueError as exc:
        raise ScanRangeError(f"Rentang tidak valid: {token}") from exc
    if int(last) < int(first):
        raise ScanRangeError(f"Rentang terbalik: {token}")
    return first, last


def _count(token: str) -> int:
    """Number of addresses **without** expanding them (used by the guard)."""
    if not token:
        return 0
    if "-" in token:
        first, last = _range_bounds(token)
        return int(last) - int(first) + 1
    network = _network(token)
    # /31 and /32: `hosts()` still returns every address, but `num_addresses - 2`
    # would be 0 or negative.
    return network.num_addresses - 2 if network.prefixlen < 31 else network.num_addresses


def _expand(token: str) -> Iterator[str]:
    if not token:
        return iter(())
    if "-" in token:
        first, last = _range_bounds(token)
        return (str(ipaddress.IPv4Address(value)) for value in range(int(first), int(last) + 1))
    return (str(host) for host in _network(token).hosts())  # /24 -> 254 hosts


def parse_hosts(specs: list[str]) -> list[str]:
    """Validated, de-duplicated addresses from CIDR / single IP / ``a-b`` ranges."""
    tokens: list[str] = []
    for spec in specs:
        for part in spec.replace(";", ",").split(","):
            if part.strip():
                tokens.append(part.strip())

    # The size is measured FIRST. Expanding `10.0.0.0/8` (16.7 million addresses)
    # before checking the cap exhausts memory long before the guard can fire.
    total = sum(_count(token) for token in tokens)
    if total == 0:
        raise ScanRangeError("Tidak ada alamat yang bisa dipindai.")
    if total > settings.scan_max_hosts:
        raise ScanRangeError(
            f"{total} alamat sekaligus terlalu banyak (batas {settings.scan_max_hosts}). "
            "Pakai rentang /24, mis. 10.100.1.0/24."
        )

    hosts: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for address in _expand(token):
            if address not in seen:
                seen.add(address)
                hosts.append(address)

    allowed = allowed_networks()
    outside = [
        host for host in hosts if not any(ipaddress.ip_address(host) in net for net in allowed)
    ]
    if outside:
        shown = ", ".join(outside[:5]) + ("..." if len(outside) > 5 else "")
        raise ScanRangeError(
            f"Alamat di luar subnet yang diizinkan ({shown}). "
            f"Subnet yang diizinkan: {settings.scan_allowed_networks}. "
            "Ubah SCAN_ALLOWED_NETWORKS kalau memang perlu memindai subnet lain."
        )
    return hosts


# ---------------------------------------------------------------------------
# TCP probe & identification
# ---------------------------------------------------------------------------
def local_address_for(target: str) -> str | None:
    """The local address that would be used to reach ``target``.

    A UDP ``connect`` sends no packet; it only asks the kernel to pick a route, and
    ``getsockname()`` then reports the source address. On a multi-homed host this
    answers "which NIC reaches 10.100.1.0/24" before blaming a panel.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def probe_host(ip: str, port: int, timeout: float, source_address: str | None = None) -> ScanHit:
    """One TCP connect. It speaks no panel protocol, so it is safe to parallelise."""
    started = time.perf_counter()
    try:
        with socket.create_connection(
            (ip, port),
            timeout=timeout,
            source_address=(source_address, 0) if source_address else None,
        ):
            return ScanHit(
                ip=ip,
                port=port,
                open=True,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )
    except TimeoutError:
        return ScanHit(ip=ip, port=port, error="timeout (firewall / tidak ada host)")
    except OSError as exc:
        return ScanHit(ip=ip, port=port, error=str(exc))


def _param(value: object) -> str | None:
    """The firmware answers a literal ``?`` for a parameter it cannot report."""
    text = "" if value is None else str(value).strip()
    return None if text in {"", "?"} else text


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class NetworkScanService:
    """Scans an IP range. ``db`` is used only to flag panels already registered."""

    def __init__(self, db=None):
        self.db = db

    # -- used by the UI as a hint ------------------------------------------
    @staticmethod
    def local_networks() -> list[dict]:
        """Each allowed subnet plus the local address used to reach it.

        ``local_address: null`` means this host has **no route** to that subnet — the
        scan will finish without findings, and that does not mean the panels are down.
        """
        result: list[dict] = []
        for token in (settings.scan_allowed_networks or "").replace(";", ",").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                network = _network(token)
            except ScanRangeError:
                continue
            probe = next(network.hosts(), network.network_address)
            result.append({"range": token, "local_address": local_address_for(str(probe))})
        return result

    # -- public API --------------------------------------------------------
    def scan(self, request) -> dict:
        """Single-answer form, used by ``POST /api/scan`` without ``stream``."""
        hosts: list[ScanHit] = []
        summary: dict = {}
        for event in self.iter_scan(request):
            if event["type"] == "host":
                hosts.append(
                    ScanHit(**{key: value for key, value in event.items() if key != "type"})
                )
            elif event["type"] == "summary":
                summary = event
        return {
            "ranges": summary.get("ranges", list(request.ranges)),
            "hosts_scanned": summary.get("hosts_scanned", 0),
            "hosts_open": summary.get("hosts_open", 0),
            "duration_ms": summary.get("duration_ms", 0.0),
            "hosts": [asdict(hit) for hit in hosts],
        }

    def iter_scan(self, request) -> Iterator[dict]:
        """``progress`` per batch, one ``host`` per panel found, then ``summary``.

        Ranges are walked one after another, so the operator sees
        "10.100.1.0/24 done" before "192.168.1.0/24" starts.
        """
        started = time.perf_counter()
        scanned = opened = 0

        for spec in request.ranges:
            hosts = parse_hosts([spec])
            local = request.local_address or local_address_for(hosts[0])
            found: list[ScanHit] = []
            done = 0

            with ThreadPoolExecutor(max_workers=request.workers) as pool:
                futures = [
                    pool.submit(probe_host, ip, request.port, request.timeout, local)
                    for ip in hosts
                ]
                for future in as_completed(futures):
                    hit = future.result()
                    done += 1
                    if hit.open:
                        found.append(hit)
                    if done % PROGRESS_CHUNK == 0 or done == len(hosts):
                        yield {
                            "type": "progress",
                            "range": spec,
                            "done": done,
                            "total": len(hosts),
                            "open": len(found),
                        }

            scanned += len(hosts)
            opened += len(found)

            # A C3 panel accepts only ONE connection at a time, so identification is
            # sequential — unlike the TCP probes above, which hold no session.
            found.sort(key=lambda hit: ipaddress.ip_address(hit.ip))
            for hit in found:
                if request.identify:
                    self._identify(hit, request.port)
                yield {"type": "host", **asdict(hit)}

        yield {
            "type": "summary",
            "ranges": list(request.ranges),
            "hosts_scanned": scanned,
            "hosts_open": opened,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }

    # -- internals ---------------------------------------------------------
    def _identify(self, hit: ScanHit, port: int) -> None:
        """Read the panel's identity. A failure here does NOT discard the TCP hit.

        An open port 4370 is worth reporting on its own: the firmware may refuse us
        (transient ``-2``/``-107``/``-112``) while the panel is perfectly alive.
        """
        client = build_client(hit.ip, port)
        try:
            client.connect()
            # No personnel count: it reads the `user` table, which is expensive and
            # tells us nothing about whether this host is a panel.
            info = client.get_info(include_personnel_count=False)
        # Deliberately broad: `DeviceError` covers the library's own failures, but a
        # half-parsed reply surfaces as whatever the parser raised. One unreadable
        # host must never abort a sweep that is 250 addresses long.
        except Exception as exc:  # noqa: BLE001
            hit.error = f"port {port} terbuka, identitas tidak terbaca: {exc}"
            return
        finally:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 - a failed disconnect must not abort the scan
                pass

        hit.serial_number = info.serial_number
        hit.device_name = info.device_name
        hit.firmware_version = info.firmware_version
        hit.lock_count = info.lock_count
        hit.model = info.model
        hit.mac_address = info.mac_address
        # Shown next to the address it answered on. This is a READ of the panel's own
        # configuration — nothing here writes it (see the Device tab for why).
        hit.panel_ip = _param(info.raw.get("IPAddress"))
        hit.panel_netmask = _param(info.raw.get("NetMask"))
        hit.panel_gateway = _param(info.raw.get("GATEWAY"))
        self._flag_registered(hit)

    def _flag_registered(self, hit: ScanHit) -> None:
        """Flag panels already in the database.

        Serial is checked **before** IP: a panel that was moved or re-addressed still
        matches by serial, and that is the only way to find it once its address
        changed. (This application still cannot fix the panel's own IP remotely —
        a wrong address makes the panel unreachable and can only be corrected on
        site; see the Device tab.)
        """
        if self.db is None:
            return
        from app.services.device_service import DeviceService

        # A database problem must not throw away a sweep that already found panels.
        try:
            devices = DeviceService(self.db).list()
        except Exception as exc:  # noqa: BLE001
            logger.warning("tidak bisa mencocokkan device terdaftar: %s", exc)
            return
        by_serial = {d.serial_number: d for d in devices if d.serial_number}
        by_ip = {d.ip: d for d in devices}
        device = (by_serial.get(hit.serial_number) if hit.serial_number else None) or by_ip.get(
            hit.ip
        )
        if device is not None:
            hit.registered = True
            hit.device_id = device.id
            hit.device_ip = device.ip
