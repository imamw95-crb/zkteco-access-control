"""Copy every row of a source database into an empty target database.

Written for the one-off move of the local SQLite ``live_check.db`` into the
PostgreSQL that ``docker-compose.yml`` runs on the deploy host, but the
implementation is generic: the source is a SQLite **file** (opened read-only, so
the live dev database cannot be touched) and the target is any SQLAlchemy URL.

The schema is never created or altered here — the target must already be at the
current Alembic head, and it must be **empty** unless ``--force`` is given.

Usage (inside the backend container, where ``DATABASE_URL`` already points at
the target):

    python -m scripts.copy_sqlite_to_postgres --source /tmp/live_check.db --dry-run
    python -m scripts.copy_sqlite_to_postgres --source /tmp/live_check.db
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import Connection

from app import models  # noqa: F401  (registers every table on Base.metadata)
from app.config import settings
from app.database import Base

#: Tables that are deliberately not copied.
#: ``auth_sessions`` holds the *source* machine's live logins; carrying them over
#: would hand out sessions on the new host that nobody asked for.
SKIP_TABLES = frozenset({"alembic_version", "auth_sessions"})

CHUNK = 500


def read_source(path: str) -> dict[str, tuple[list[str], list[tuple]]]:
    """Read every table of a SQLite file, read-only."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise SystemExit(f"sumber tidak ada: {resolved}")
    con = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in con.execute(
                "select name from sqlite_master where type='table' order by name"
            )
        ]
        data: dict[str, tuple[list[str], list[tuple]]] = {}
        for name in names:
            if name.startswith("sqlite_"):
                continue
            cursor = con.execute(f'select * from "{name}"')
            columns = [col[0] for col in cursor.description]
            data[name] = (columns, [tuple(row) for row in cursor.fetchall()])
        return data
    finally:
        con.close()


def parse_datetime(value: object) -> datetime:
    """Turn a SQLite timestamp (string, usually ISO) into an aware datetime.

    Naive values are assumed UTC, which is what ``models.utcnow()`` writes.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def coerce(value: object, column_type: sa.types.TypeEngine) -> object:
    """SQLite hands everything back as str/int/bytes; PostgreSQL is strict."""
    if value is None:
        return None
    if isinstance(column_type, sa.DateTime):
        return parse_datetime(value)
    if isinstance(column_type, sa.Boolean):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "t", "yes"}
    if isinstance(column_type, (sa.Integer, sa.BigInteger, sa.SmallInteger)):
        return int(value)
    if isinstance(column_type, (sa.Float, sa.Numeric)):
        return float(value)
    if isinstance(column_type, (sa.String, sa.Text)):
        return str(value)
    return value


def reset_sequences(conn: Connection, table: sa.Table) -> None:
    """Point each integer PK sequence past the highest copied id."""
    for column in table.columns:
        if not (column.primary_key and isinstance(column.type, sa.Integer)):
            continue
        sequence = conn.execute(
            text("select pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": column.name}
        ).scalar()
        if not sequence:
            continue
        highest = conn.execute(text(f'select max("{column.name}") from "{table.name}"')).scalar()
        if highest is None:
            conn.execute(text("select setval(:s, 1, false)"), {"s": sequence})
        else:
            conn.execute(text("select setval(:s, :v)"), {"s": sequence, "v": int(highest)})


def copy_rows(
    source_path: str,
    target_engine: Engine,
    *,
    force: bool = False,
    dry_run: bool = False,
    log=print,
) -> dict[str, int]:
    """Copy the source into the target. Returns ``{table: rows_written}``."""
    data = read_source(source_path)
    written: dict[str, int] = {}

    with target_engine.begin() as conn:
        present = set(inspect(conn).get_table_names())
        for table in Base.metadata.sorted_tables:
            name = table.name
            if name in SKIP_TABLES:
                log(f"{name:28} dilewati (sengaja)")
                continue
            if name not in present:
                raise RuntimeError(
                    f"tabel target '{name}' tidak ada — jalankan alembic upgrade head"
                )
            if name not in data:
                log(f"{name:28} tidak ada di sumber, dilewati")
                written[name] = 0
                continue

            source_columns, rows = data[name]
            shared = [column for column in table.columns if column.name in set(source_columns)]
            kept = {column.name for column in shared}
            dropped = sorted(set(source_columns) - kept)
            if dropped:
                log(f"{name:28} kolom sumber diabaikan: {', '.join(dropped)}")
            if not rows:
                log(f"{name:28} 0 baris")
                written[name] = 0
                continue
            if dry_run:
                log(f"{name:28} {len(rows)} baris siap disalin (dry run)")
                written[name] = len(rows)
                continue

            payload = [
                {
                    column.name: coerce(row[source_columns.index(column.name)], column.type)
                    for column in shared
                }
                for row in rows
            ]

            existing = conn.execute(text(f'select count(*) from "{name}"')).scalar() or 0
            if existing:
                if not force:
                    raise RuntimeError(
                        f"tabel target '{name}' sudah berisi {existing} baris — "
                        "pakai --force untuk mengosongkannya lebih dulu"
                    )
                conn.execute(text(f'delete from "{name}"'))

            conn.execute(sa.insert(table), payload[:CHUNK])
            for start in range(CHUNK, len(payload), CHUNK):
                conn.execute(sa.insert(table), payload[start : start + CHUNK])

            # Only PostgreSQL has the sequences that explicit ids walk past.
            # (SQLite picks next_insert_id itself.)
            if target_engine.dialect.name == "postgresql":
                reset_sequences(conn, table)
            log(f"{name:28} {len(payload)} baris")
            written[name] = len(payload)

    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="path ke berkas SQLite sumber")
    parser.add_argument(
        "--target",
        default=settings.database_url,
        help="URL SQLAlchemy tujuan (default: DATABASE_URL)",
    )
    parser.add_argument("--force", action="store_true", help="kosongkan tabel tujuan lebih dulu")
    parser.add_argument("--dry-run", action="store_true", help="hitung saja, jangan menulis")
    args = parser.parse_args(argv)

    target = create_engine(args.target, future=True)
    log = print
    log(f"sumber : {args.source}")
    log(f"tujuan : {target.url.render_as_string(hide_password=True)}")
    if args.dry_run:
        mode = "DRY RUN"
    elif args.force:
        mode = "FORCE (tabel tujuan dikosongkan)"
    else:
        mode = "aman (tabel tujuan harus kosong)"
    log(f"mode   : {mode}")
    written = copy_rows(args.source, target, force=args.force, dry_run=args.dry_run, log=log)
    total = sum(written.values())
    log(f"total  : {total} baris{' (dry run, tidak ada yang ditulis)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
