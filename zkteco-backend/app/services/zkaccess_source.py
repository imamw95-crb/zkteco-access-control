"""Read-only reader for a legacy ZKAccess SQL Server database.

Used to migrate personnel, departments, access groups and device metadata out of
ZKAccess 3.5 into this backend. ZKAccess stores the *same* login number in
``USERINFO.Badgenumber`` that the panels use as their PIN, which is what makes a
faithful migration possible at all.

Safety: every statement goes through :meth:`ZKAccessSource._query`, which
refuses anything that is not a ``SELECT``/``WITH`` statement. The importer can
therefore only ever read from the hospital's production database.

Credentials come from the environment (never hard-coded):

    ZKACCESS_SERVER=10.100.1.100\\SQLEXPRESS
    ZKACCESS_DATABASE=ZKAccess
    ZKACCESS_USER=sa
    ZKACCESS_PASSWORD=...
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DRIVER = "ODBC Driver 11 for SQL Server"


class ZKAccessSourceError(RuntimeError):
    pass


class WriteAttemptedError(ZKAccessSourceError):
    """Raised if a caller tries to run a non-SELECT statement."""


@dataclass
class ZKAccessConfig:
    server: str
    database: str
    user: str
    password: str
    driver: str = DEFAULT_DRIVER
    timeout: int = 30

    @classmethod
    def from_env(cls, **overrides: Any) -> ZKAccessConfig:
        values = {
            "server": os.environ.get("ZKACCESS_SERVER", ""),
            "database": os.environ.get("ZKACCESS_DATABASE", "ZKAccess"),
            "user": os.environ.get("ZKACCESS_USER", ""),
            "password": os.environ.get("ZKACCESS_PASSWORD", ""),
            "driver": os.environ.get("ZKACCESS_DRIVER", DEFAULT_DRIVER),
        }
        values.update({k: v for k, v in overrides.items() if v})
        missing = [k for k in ("server", "user", "password") if not values.get(k)]
        if missing:
            raise ZKAccessSourceError(
                "Konfigurasi ZKAccess belum lengkap: "
                + ", ".join(missing)
                + ". Set environment ZKACCESS_SERVER / ZKACCESS_USER / ZKACCESS_PASSWORD."
            )
        return cls(**values)

    def connection_string(self) -> str:
        return (
            f"DRIVER={{{self.driver}}};"
            f"SERVER={self.server};"
            f"DATABASE={self.database};"
            f"UID={self.user};"
            f"PWD={self.password};"
            "TrustServerCertificate=yes"
        )

    def __repr__(self) -> str:  # never leak the password into logs
        return (
            f"ZKAccessConfig(server={self.server!r}, database={self.database!r}, "
            f"user={self.user!r}, password=***)"
        )


class ZKAccessSource:
    """Read-only access to a ZKAccess database."""

    def __init__(self, config: ZKAccessConfig):
        self.config = config
        self._conn = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> ZKAccessSource:
        try:
            import pyodbc
        except ImportError as exc:  # pragma: no cover
            raise ZKAccessSourceError(
                "Butuh pyodbc: pip install pyodbc (dan ODBC Driver for SQL Server)"
            ) from exc

        try:
            self._conn = pyodbc.connect(
                self.config.connection_string(), timeout=self.config.timeout
            )
        except Exception as exc:
            raise ZKAccessSourceError(f"Gagal connect ke {self.config.server}: {exc}") from exc
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> ZKAccessSource:
        return self.connect()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- querying ----------------------------------------------------------
    @staticmethod
    def _assert_read_only(sql: str) -> None:
        stripped = sql.lstrip().lstrip("(").upper()
        if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
            raise WriteAttemptedError(
                "Hanya SELECT/WITH yang diizinkan; query ini ditolak untuk "
                "melindungi database ZKAccess produksi."
            )

    def _query(self, sql: str, params: tuple | None = None) -> list[dict]:
        self._assert_read_only(sql)
        if self._conn is None:
            raise ZKAccessSourceError("Belum connect. Panggil connect() dulu.")

        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, params or ())
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    # -- data --------------------------------------------------------------
    def server_version(self) -> str:
        rows = self._query("SELECT @@VERSION AS v")
        return rows[0]["v"].splitlines()[0] if rows else "unknown"

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for table in (
            "USERINFO",
            "DEPARTMENTS",
            "Machines",
            "acc_door",
            "acc_levelset",
            "acc_levelset_emp",
            "acc_levelset_door_group",
        ):
            rows = self._query(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = rows[0]["n"]
        return counts

    def devices(self) -> list[dict]:
        """Panels, keyed by IP — maps onto our ``Device`` table."""
        return self._query(
            """
            SELECT ID, MachineAlias, IP, Port, sn, FirmwareVersion,
                   usercount, door_count, reader_count, max_user_count,
                   CommPassword, device_name
            FROM Machines
            ORDER BY ID
            """
        )

    def doors(self) -> list[dict]:
        return self._query(
            """
            SELECT d.id, d.device_id, d.door_no, d.door_name, m.IP AS device_ip
            FROM acc_door d
            LEFT JOIN Machines m ON m.ID = d.device_id
            ORDER BY d.device_id, d.door_no
            """
        )

    def departments(self) -> list[dict]:
        return self._query(
            """
            SELECT DEPTID, DEPTNAME, SUPDEPTID, code
            FROM DEPARTMENTS
            ORDER BY DEPTID
            """
        )

    def personnel(self) -> list[dict]:
        """Users as they exist on the panels.

        ``Badgenumber`` is the PIN the panel stores, so it becomes our
        ``employee_id`` and ``pin``. ``USERID`` is only an internal ZKAccess
        surrogate and is returned so access groups can be joined.
        """
        return self._query(
            """
            SELECT u.USERID, u.Badgenumber, u.Name, u.lastname, u.Gender,
                   u.CardNo, u.DEFAULTDEPTID, d.DEPTNAME,
                   u.acc_startdate, u.acc_enddate, u.privilege
            FROM USERINFO u
            LEFT JOIN DEPARTMENTS d ON d.DEPTID = u.DEFAULTDEPTID
            ORDER BY u.USERID
            """
        )

    def access_groups(self) -> list[dict]:
        return self._query(
            """
            SELECT id, level_name, level_timeseg_id
            FROM acc_levelset
            ORDER BY id
            """
        )

    def group_doors(self) -> list[dict]:
        """Which (device, door) each access group covers."""
        return self._query(
            """
            SELECT g.acclevelset_id, g.accdoor_id, d.device_id, d.door_no,
                   m.IP AS device_ip
            FROM acc_levelset_door_group g
            LEFT JOIN acc_door d ON d.id = g.accdoor_id
            LEFT JOIN Machines m ON m.ID = d.device_id
            ORDER BY g.acclevelset_id, g.accdoor_id
            """
        )

    def group_members(self) -> list[dict]:
        """Which person (USERID) belongs to which access group."""
        return self._query(
            """
            SELECT e.acclevelset_id, e.employee_id, u.Badgenumber
            FROM acc_levelset_emp e
            LEFT JOIN USERINFO u ON u.USERID = e.employee_id
            ORDER BY e.acclevelset_id, e.employee_id
            """
        )
