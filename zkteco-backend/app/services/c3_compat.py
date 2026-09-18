"""Compatibility shims for the `zkaccess-c3` library.

Two real-world quirks were reproduced against C3-100/C3-200 panels running
firmware ``AC Ver 5.4.3.2001`` (Sep 2019 / Feb 2023 / Feb 2025 builds). Both are
bugs in the upstream library, not in our code:

1. ``_DataTableCfg.__init__`` assumes the first entry of every DATATABLE_CFG
   line is an integer table index. These panels emit an extra trailing line::

       logfmt=256,logfmt=i1

   ``_parse_kv_from_message`` collapses the duplicate ``logfmt`` key into a
   single dict entry, so the index becomes the string ``"i1"`` and
   ``int("i1")`` raises ``ValueError``. Because ``get_device_data()`` parses the
   *whole* table configuration before reading any table, this single bad line
   makes **every** table read fail with
   ``invalid literal for int() with base 10: 'i1'``.

   Fix: skip configuration lines whose index is not numeric.

2. ``get_device_data()`` is unreliable when several fields are requested at
   once. On these panels a request for >= 3 fields (and always for the
   ``transaction`` table) is answered with a 17-byte status payload whose first
   byte is ``0`` instead of the table index, which the library reports as
   ``ValueError: Wrong table returned by panel``. Reading a single field at a
   time is reliable, so :func:`read_table_robust` degrades to one-field reads.

Applying the patches is idempotent and safe to call at import time.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PATCH_ATTR = "_zkteco_compat_patched"


def apply_patches() -> bool:
    """Patch the upstream library in-place. Idempotent.

    Returns ``True`` if the patches are in place, ``False`` if the library is
    not importable (e.g. during unit tests with a mocked client).
    """
    try:
        from c3 import consts, core
    except ImportError:  # pragma: no cover - library optional at test time
        logger.debug("zkaccess-c3 not installed; compatibility patches skipped")
        return False

    if getattr(core.C3, _PATCH_ATTR, False):
        return True

    def _tolerant_get_device_data_cfg(self):
        """DATATABLE_CFG parser that skips malformed lines."""
        message, _ = self._send_receive(consts.Command.DATATABLE_CFG)
        cfg = []
        for line in message.split(b"\x0a"):
            kv = self._parse_kv_from_message(line)
            if not kv:
                continue
            name, index = next(iter(kv.items()))
            if not str(index).strip().isdigit():
                logger.debug("skipping malformed table config line: %s=%r", name, index)
                continue
            cfg.append(core._DataTableCfg(kv))
        return cfg

    core.C3._get_device_data_cfg = _tolerant_get_device_data_cfg
    core.C3._receive = _make_exact_receive(core, consts)
    setattr(core.C3, _PATCH_ATTR, True)
    logger.info("Applied zkaccess-c3 compatibility patches")
    return True


def _make_exact_receive(core, consts):
    """Build a `_receive` replacement that reads full TCP frames.

    Upstream does a single ``sock.recv(data_size + 3)``. TCP ``recv`` returns
    *up to* the requested byte count, so any reply that spans more than one
    segment comes back short and parsing dies with
    ``Payload does not include message end marker``. That is what happens on
    busy panels: reading `userauthorize` from a panel with 525 users fails,
    while the same read on a panel with 27 users succeeds.

    The replacement keeps the upstream validation and checksum logic, but reads
    the header and payload with exact-length loops.
    """

    def _recv_exact(sock, count: int, timeout: float) -> bytes:
        sock.settimeout(timeout)
        chunks: list[bytes] = []
        remaining = count
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise ConnectionError(
                    f"Device closed the connection while {remaining} byte(s) "
                    "of the reply were still outstanding"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _receive(self):  # noqa: ANN001 - mirrors the upstream signature
        self._sock.settimeout(self.receive_timeout)

        header = b""
        for _ in range(max(1, int(self.receive_retries))):
            try:
                header = self._sock.recv(5)
                if len(header) == 5:
                    break
            except TimeoutError:
                continue

        if len(header) != 5:
            raise ConnectionError(
                f"Invalid response header received; expected 5 bytes, received {header}"
            )

        received_command, data_size, protocol_version = self._get_message_header(header)

        # Exact read: the reply may span several TCP segments.
        tail = _recv_exact(self._sock, data_size + 3, self.receive_timeout)

        message = bytearray()
        if data_size > 0:
            message = self._get_message(header + tail)

        if len(message) != data_size:
            raise ValueError(
                f"Length of received message ({len(message)}) doesn't match specified ({data_size})"
            )

        if received_command == consts.C3_REPLY_ERROR:
            from c3 import utils

            error = utils.byte_to_signed_int(message[-1])
            raise ConnectionError(
                f"Error {error} received in reply: "
                f"{consts.Errors[error] if error in consts.Errors else 'Unknown'}"
            )

        return message, data_size, protocol_version

    return _receive


#: Raised when the panel answers a data request with a non-data payload.
class PanelRefusedError(RuntimeError):
    pass


def read_table_robust(panel, table: str, fields: list[str] | None = None) -> list[dict]:
    """Read a device data table, degrading to single-field reads if needed.

    Returns a list of records. Field values are merged by record position.
    """
    if not fields:
        # No explicit fields: the library will ask for every configured field,
        # which is what triggers the >= 3 field bug. Read one field at a time.
        try:
            cfg = panel._get_device_data_cfg()
        except Exception:  # pragma: no cover - defensive
            cfg = []
        table_cfg = next((c for c in cfg if c.name == table), None)
        if table_cfg is None:
            raise PanelRefusedError(f"Table '{table}' is not available on this panel")
        fields = [f.name for f in table_cfg.fields]

    # Fast path: a single field, or a small multi-field request that this panel
    # may well accept.
    if len(fields) <= 2:
        try:
            return panel.get_device_data(table, fields)
        except ValueError as exc:
            if "Wrong table returned" not in str(exc):
                raise
            logger.debug("multi-field read of %s refused, falling back: %s", table, exc)

    # Slow path: one field per request, merged by record index.
    merged: dict[int, dict] = {}
    for field in fields:
        rows = panel.get_device_data(table, [field])
        for idx, row in enumerate(rows):
            merged.setdefault(idx, {}).update(row)

    return [merged[i] for i in sorted(merged)]


def count_records_robust(panel, table: str, key_field: str) -> int:
    """Count records in ``table`` by reading only ``key_field``.

    This is the reliable way to get the *actual* number of users stored on a
    panel, as opposed to ``~MaxUserCount`` which is only the capacity.
    """
    return len(panel.get_device_data(table, [key_field]))
