"""Inspect ZKAccess exports (Personnel / Department .xls) and a ZKAccess .bak.

Read-only. Used as a pre-flight before migrating ZKAccess data into the new
backend, so that data problems (duplicate cards, orphan departments, blank
names) surface *before* anything is written to the database.

    python -m scripts.inspect_zkaccess_export --personnel Personnel.xls \
        --departments Departmenet.xls --backup "backup new.bak"
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

MAX_SAMPLE = 10


def _read_sheet(path: Path) -> tuple[list[str], list[list[str]]]:
    try:
        import xlrd
    except ImportError:
        raise SystemExit("Butuh xlrd untuk membaca .xls:  pip install xlrd") from None

    book = xlrd.open_workbook(str(path))
    sheet = book.sheet_by_index(0)
    rows: list[list[str]] = []
    for r in range(sheet.nrows):
        rows.append([_cell(c.value) for c in sheet.row(r)])
    header = [h.strip() for h in rows[0]] if rows else []
    return header, rows[1:]


def _cell(value) -> str:
    """xlrd returns floats for numeric cells; render 1.0 as '1'."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _looks_like_header(row: list[str]) -> bool:
    return any("name" in cell.lower() for cell in row)


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def inspect_personnel(path: Path) -> dict:
    header, rows = _read_sheet(path)
    rows = [r for r in rows if any(c for c in r)]

    section(f"PERSONNEL - {path.name}")
    print(f"Kolom ({len(header)}): {', '.join(header)}")
    print(f"Baris data: {len(rows)}")

    def col(name: str) -> int:
        wanted = name.lower().replace(" ", "")
        for i, h in enumerate(header):
            if h.lower().replace(" ", "") == wanted:
                return i
        return -1

    i_id = col("Personnel ID")
    i_first = col("First Name")
    i_last = col("Last Name")
    i_card = col("Card Number")
    i_depname = col("Department Name")
    i_gender = col("Gender")

    def get(row: list[str], idx: int) -> str:
        return row[idx].strip() if 0 <= idx < len(row) else ""

    ids = [get(r, i_id) for r in rows]
    cards = [get(r, i_card) for r in rows]

    print("\n-- Identitas --")
    print(f"  Personnel ID unik : {len({i for i in ids if i})}")
    print(f"  Personnel ID kosong: {sum(1 for i in ids if not i)}")

    id_dupes = [k for k, v in Counter(i for i in ids if i).items() if v > 1]
    print(
        f"  Personnel ID duplikat: {len(id_dupes)}"
        + (f" -> {id_dupes[:MAX_SAMPLE]}" if id_dupes else "")
    )

    print("\n-- Card Number --")
    with_card = [c for c in cards if c and c != "0"]
    print(f"  Punya kartu       : {len(with_card)}")
    print(f"  Tanpa kartu (0/-) : {len(cards) - len(with_card)}")

    card_dupes = {k: v for k, v in Counter(with_card).items() if v > 1}
    print(f"  Card duplikat     : {len(card_dupes)}")
    if card_dupes:
        print("    (kartu duplikat = dua orang bisa membuka pintu yang sama!)")
        for card, count in list(card_dupes.items())[:MAX_SAMPLE]:
            holders = [
                f"{get(r, i_id)}:{get(r, i_first)} {get(r, i_last)}".strip()
                for r in rows
                if get(r, i_card) == card
            ]
            print(f"      kartu {card} dipakai {count}x -> {holders}")

    bad_len = [c for c in with_card if not c.isdigit()]
    if bad_len:
        print(f"  Card non-numerik  : {len(bad_len)} -> {bad_len[:MAX_SAMPLE]}")

    print("\n-- Nama --")
    blank_both = sum(1 for r in rows if not get(r, i_first) and not get(r, i_last))
    print(f"  Nama kosong       : {blank_both}")

    print("\n-- Departemen --")
    dep_counts = Counter(get(r, i_depname) or "(kosong)" for r in rows)
    print(f"  Jumlah departemen : {len(dep_counts)}")
    for name, count in dep_counts.most_common():
        print(f"      {name:<28} {count:>4}")

    if i_gender >= 0:
        print("\n-- Gender --")
        for name, count in Counter(get(r, i_gender) or "(kosong)" for r in rows).most_common():
            print(f"      {name:<12} {count:>4}")

    return {
        "rows": len(rows),
        "ids": ids,
        "cards": with_card,
        "card_dupes": card_dupes,
        "departments": dep_counts,
    }


def inspect_departments(path: Path) -> dict:
    header, rows = _read_sheet(path)
    rows = [r for r in rows if any(c for c in r)]

    section(f"DEPARTMENTS - {path.name}")
    print(f"Kolom: {', '.join(header)}")
    print(f"Baris data: {len(rows)}")

    names = {r[1].strip() for r in rows if len(r) > 1 and r[1].strip()}
    roots = [r[1].strip() for r in rows if len(r) > 2 and not r[2].strip()]

    print(f"\n  Nama departemen unik: {len(names)}")
    print(f"  Root (tanpa induk)  : {roots}")

    orphans = [
        r[1].strip() for r in rows if len(r) > 2 and r[2].strip() and r[2].strip() not in names
    ]
    print(f"  Induk tidak dikenal : {orphans if orphans else 'tidak ada'}")

    for r in rows[:MAX_SAMPLE]:
        parent = r[2].strip() if len(r) > 2 else ""
        print(f"      {r[0]:>3}  {r[1]:<28} {('<- ' + parent) if parent else '(root)'}")

    return {"names": names}


def inspect_backup(path: Path) -> None:
    section(f"BACKUP - {path.name}")
    size = path.stat().st_size
    print(f"  Ukuran  : {size:,} bytes ({size / 1024 / 1024:.2f} MB)")

    with path.open("rb") as handle:
        head = handle.read(256 * 1024)
        handle.seek(max(0, size - 64 * 1024))
        tail = handle.read()

    print(
        f"  Magic   : {head[:4]!r} -> "
        f"{'SQL Server backup (MTF)' if head[:4] == b'TAPE' else 'bukan MTF'}"
    )

    # SQL Server writes the MTF descriptor as UTF-16LE, so an ASCII-only search
    # silently finds nothing. Search both decodings.
    ascii_text = head.decode("ascii", "ignore")
    utf16_text = head.decode("utf-16-le", "ignore")
    haystack = ascii_text + "\n" + utf16_text

    mdf = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\.mdf", utf16_text) or re.findall(
        r"([A-Za-z_][A-Za-z0-9_]*)\.mdf", ascii_text
    )
    if mdf:
        print(f"  Database      : {mdf[0]}")

    ldf = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\.ldf", utf16_text)
    if ldf:
        print(f"  Logical file  : {sorted(set(mdf + ldf))}")

    servers = re.findall(r"([A-Za-z0-9][A-Za-z0-9\-]{2,})\\SQLEXPRESS", haystack)
    if mdf:
        # The MTF descriptor concatenates the database name directly before the
        # server name ("ZKAccessDESKTOP-2P26H68"), so strip that known prefix.
        db_name = mdf[0]
        servers = [
            s[len(db_name) :] if s.startswith(db_name) and len(s) > len(db_name) else s
            for s in servers
        ]
    if servers:
        print(f"  Server asal   : {sorted(set(servers))}")

    version = re.search(r"MSSQL(\d+)", haystack)
    if version:
        print(
            f"  SQL Server    : MSSQL{version.group(1)} "
            f"({'2022' if version.group(1) == '16' else '?'})"
        )

    tail_nonzero = len(tail.strip(b"\x00"))
    print(f"  Ekor file     : {tail_nonzero} byte non-nol")

    print("\n  Catatan: isi tabel TIDAK bisa dibaca tanpa di-restore ke SQL Server.")
    print("           Butuh instance SQL Server + hak akses (lihat rekomendasi).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--personnel", type=Path)
    parser.add_argument("--departments", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args(argv)

    if not any([args.personnel, args.departments, args.backup]):
        parser.print_help()
        return 2

    result: dict = {}

    if args.departments:
        if args.departments.exists():
            result["dept"] = inspect_departments(args.departments)
        else:
            print(f"Tidak ditemukan: {args.departments}", file=sys.stderr)

    if args.personnel:
        if args.personnel.exists():
            result["person"] = inspect_personnel(args.personnel)
        else:
            print(f"Tidak ditemukan: {args.personnel}", file=sys.stderr)

    if args.backup and args.backup.exists():
        inspect_backup(args.backup)

    # Cross-check: departments used by personnel but absent from the department list
    person = result.get("person")
    dept = result.get("dept")
    if person and dept:
        used = {d for d in person["departments"] if d and d != "(kosong)"}
        missing = sorted(used - dept["names"])
        section("CROSS-CHECK")
        print("  Departemen dipakai personel tapi tidak ada di daftar departemen:")
        print(f"    {missing if missing else 'tidak ada - data konsisten'}")

    section("RINGKASAN")
    if person:
        print(f"  Personel          : {person['rows']}")
        print(f"  Kartu duplikat    : {len(person['card_dupes'])}")
        print(f"  Departemen dipakai: {len(person['departments'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
