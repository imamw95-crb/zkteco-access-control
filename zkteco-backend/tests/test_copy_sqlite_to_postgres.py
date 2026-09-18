"""Tests for `scripts/copy_sqlite_to_postgres.py`.

Both sides are throwaway SQLite files in `tmp_path` — nothing here touches
`live_check.db` or the real deployment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccessGroup,
    AccessGroupDoor,
    AccessLog,
    AccessTimeZone,
    AuthSession,
    Department,
    Device,
    Personnel,
    PersonnelAccessGroup,
    SyncRun,
    User,
    utcnow,
)
from scripts.copy_sqlite_to_postgres import (
    coerce,
    copy_rows,
    parse_datetime,
    read_source,
    reset_sequences,
)


def _make_source(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # A source-only table the script must ignore: the target keeps its own head.
        conn.execute(text("create table alembic_version (version_num varchar(32) not null)"))
        conn.execute(text("insert into alembic_version values ('a1d4c7b90e35')"))

    now = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(AccessTimeZone(id=1, name="24 Jam", device_timezone_id=1, is_24_hour=True))
        session.add(Department(id=1, name="GIZI", code="GZ"))
        session.add(AccessGroup(id=1, name="TEKNISI/IT", device_timezone_id=1, door_numbers="1,2"))
        session.add(AccessGroup(id=2, name="AKSES UMUM", device_timezone_id=1, door_numbers="1"))
        session.add(
            Device(
                id=1,
                name="RUANGAN SERVER",
                ip="10.100.1.3",
                port=4370,
                is_online=True,
                created_at=now,  # fixed on purpose: the copy must preserve it
            )
        )
        session.add(Device(id=2, name="GIZI BELAKANG", ip="10.100.1.12", is_active=True))
        session.add(
            Personnel(id=1, employee_id="531", name="Fega", card_number="2150141426", pin="531")
        )
        session.add(
            Personnel(
                id=2,
                employee_id="532",
                name="Tanpa Kartu",
                department_id=1,
                is_active=False,
                valid_until=now + timedelta(days=30),
                access_group_id=2,
            )
        )
        session.add(AccessGroupDoor(id=1, access_group_id=1, device_id=1, door_number=1))
        session.add(AccessGroupDoor(id=2, access_group_id=1, device_id=2, door_number=2))
        session.add(PersonnelAccessGroup(id=1, personnel_id=1, access_group_id=1))
        session.add(PersonnelAccessGroup(id=2, personnel_id=1, access_group_id=2))
        session.add(
            AccessLog(
                id=1,
                device_id=1,
                event_time=now,
                event_type="Masuk",
                card_number="2150141426",
                dedupe_key="abc123",
                raw='{"Pin": "531"}',
            )
        )
        session.add(SyncRun(id=1, job="fleet_sync", device_id=1, success=True, items_processed=43))
        session.add(
            User(
                id=1,
                username="admin",
                password_hash="pbkdf2$deadbeef",
                role="admin",
                must_change_password=True,
            )
        )
        session.add(
            AuthSession(
                id=1,
                token_hash="f" * 64,
                user_id=1,
                expires_at=now + timedelta(hours=12),
            )
        )
        session.commit()
    engine.dispose()


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    path = tmp_path / "source.db"
    _make_source(path)
    return path


@pytest.fixture
def target_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _count(engine, table: str) -> int:
    with engine.begin() as conn:
        return conn.execute(text(f'select count(*) from "{table}"')).scalar()


def test_read_source_is_read_only_and_lists_every_table(source_db: Path) -> None:
    before = source_db.read_bytes()
    data = read_source(str(source_db))
    assert "devices" in data
    assert "alembic_version" in data  # read, then skipped by copy_rows
    assert source_db.read_bytes() == before


def test_read_source_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        read_source(str(tmp_path / "tidak-ada.db"))
    assert not (tmp_path / "tidak-ada.db").exists()


def test_copy_moves_every_row_and_skips_session_tables(source_db: Path, target_engine) -> None:
    written = copy_rows(str(source_db), target_engine, log=lambda *_: None)

    assert written["devices"] == 2
    assert written["personnel"] == 2
    assert written["personnel_access_groups"] == 2
    assert written["access_group_doors"] == 2
    assert written["access_logs"] == 1
    assert "auth_sessions" not in written  # never carried over
    assert "alembic_version" not in written

    assert _count(target_engine, "devices") == 2
    assert _count(target_engine, "personnel") == 2
    assert _count(target_engine, "users") == 1
    assert _count(target_engine, "auth_sessions") == 0


def utc(value: datetime) -> datetime:
    """Compare the instant, not the shape: PostgreSQL returns aware datetimes,
    SQLite (used as the copy target in these tests) returns naive ones."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def test_copied_values_survive_the_type_change(source_db: Path, target_engine) -> None:
    """SQLite gives back strings/ints; the models must read back as real types."""
    copy_rows(str(source_db), target_engine, log=lambda *_: None)

    with Session(target_engine) as session:
        device = session.get(Device, 1)
        assert device is not None
        assert device.name == "RUANGAN SERVER"
        assert device.port == 4370
        assert device.is_online is True
        assert utc(device.created_at) == datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)

        person = session.get(Personnel, 2)
        assert person is not None
        assert person.department == "GIZI"  # FK to the copied department
        assert person.is_active is False
        assert utc(person.valid_until) == datetime(2026, 10, 18, 9, 30, tzinfo=timezone.utc)

        log = session.scalar(select(AccessLog))
        assert log is not None
        assert utc(log.event_time) == datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)

        user = session.get(User, 1)
        assert user is not None
        assert user.must_change_password is True

        # The authoritative access-rights tables keep their links.
        assert session.get(PersonnelAccessGroup, 1).access_group_id == 1
        assert session.get(AccessGroupDoor, 2).door_number == 2
        assert len(session.get(Personnel, 1).access_group_ids) == 2


def test_copy_refuses_a_non_empty_target_but_force_overwrites(
    source_db: Path, target_engine
) -> None:
    copy_rows(str(source_db), target_engine, log=lambda *_: None)

    with pytest.raises(RuntimeError, match="sudah berisi"):
        copy_rows(str(source_db), target_engine, log=lambda *_: None)

    again = copy_rows(str(source_db), target_engine, force=True, log=lambda *_: None)
    assert again["devices"] == 2
    assert _count(target_engine, "devices") == 2  # replaced, not appended


def test_dry_run_writes_nothing(source_db: Path, target_engine) -> None:
    planned = copy_rows(str(source_db), target_engine, dry_run=True, log=lambda *_: None)

    assert planned["devices"] == 2
    assert _count(target_engine, "devices") == 0
    assert _count(target_engine, "personnel") == 0


def test_copy_reports_an_empty_source_table(source_db: Path, target_engine) -> None:
    written = copy_rows(str(source_db), target_engine, log=lambda *_: None)
    assert written["sync_runs"] == 1
    assert written["door_schedules"] == 0  # nothing to copy, not an error


def test_missing_target_table_is_an_error_not_a_silent_skip(
    source_db: Path, tmp_path: Path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'kosong.db'}")
    with engine.begin() as conn:
        conn.execute(text("create table devices (id integer primary key, name varchar(128))"))
    try:
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            copy_rows(str(source_db), engine, log=lambda *_: None)
    finally:
        engine.dispose()


def test_utcnow_is_what_the_copy_assumes() -> None:
    """`parse_datetime` treats a naive value as UTC — that is only right while
    the models keep writing aware UTC timestamps."""
    assert utcnow().tzinfo is not None


def test_parse_datetime_reads_both_sqlite_shapes() -> None:
    expected = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    assert parse_datetime("2026-09-18 09:30:00.000000") == expected  # naive -> UTC
    assert parse_datetime("2026-09-18T09:30:00Z") == expected
    assert parse_datetime("2026-09-18 09:30:00+00:00") == expected
    aware = datetime(2026, 9, 18, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    assert parse_datetime(aware) == expected  # kept as the same instant


def test_coerce_translates_what_sqlite_hands_back() -> None:
    assert coerce("1", sa.Boolean()) is True
    assert coerce("0", sa.Boolean()) is False
    assert coerce("4370", sa.Integer()) == 4370
    assert coerce(None, sa.Integer()) is None
    assert coerce(531, sa.String()) == "531"
    assert coerce("10.5", sa.Float()) == 10.5


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _FakeConn:
    """Records the SQL `reset_sequences` emits, without needing PostgreSQL.

    The sequence fix only runs on a PostgreSQL target, so the SQLite tests above
    never reach it — this keeps that branch tested anyway.
    """

    def __init__(self, sequence: str | None = "devices_id_seq", highest: object = 7) -> None:
        self.sequence = sequence
        self.highest = highest
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):  # noqa: ANN001, ANN201
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "pg_get_serial_sequence" in sql:
            return _Result(self.sequence)
        if sql.startswith("select max("):
            return _Result(self.highest)
        return _Result(None)


def test_reset_sequences_moves_the_counter_past_the_copied_ids() -> None:
    conn = _FakeConn(highest=7)
    reset_sequences(conn, Device.__table__)

    setval = [params for sql, params in conn.calls if "setval" in sql]
    assert setval == [{"s": "devices_id_seq", "v": 7}]


def test_reset_sequences_restarts_an_empty_table_at_one() -> None:
    conn = _FakeConn(highest=None)
    reset_sequences(conn, Device.__table__)

    setval = [(sql, params) for sql, params in conn.calls if "setval" in sql]
    assert len(setval) == 1
    sql, params = setval[0]
    # `setval(seq, 1, false)` — the next id handed out is 1, not 2.
    assert "false" in sql
    assert params == {"s": "devices_id_seq"}


def test_reset_sequences_skips_a_table_without_a_sequence() -> None:
    conn = _FakeConn(sequence=None)
    reset_sequences(conn, Device.__table__)

    assert [sql for sql, _ in conn.calls if "setval" in sql] == []
