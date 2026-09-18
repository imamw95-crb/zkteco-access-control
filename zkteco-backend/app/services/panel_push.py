"""Build the records a panel expects, from our own database.

A panel stores access control in three of its own tables (indexes read live from
firmware AC Ver 5.4.3.2001 — see `scripts/inspect_panel_tables.py`):

    user          (index 1)  Pin, CardNo, Name, Group, StartTime, EndTime, ...
    userauthorize (index 2)  Pin, AuthorizeTimezoneId, AuthorizeDoorId
    timezone      (index 4)  TimezoneId, {Sun..Sat}Time{1,2,3}, {Hol1..3}Time{1,2,3}

`userauthorize` has no device column: the table lives *inside* each panel and its
`AuthorizeDoorId` is a 4-bit mask for that panel's own doors. So one person can
enjoy access through several panels, and each panel needs its own row saying
"this Pin may open doors X and Y here, during timezone Z".

That maps straight onto what we already model:

    Pin                  <- Personnel.employee_id
    CardNo               <- Personnel.card_number
    AuthorizeTimezoneId  <- AccessGroup.device_timezone_id
    AuthorizeDoorId      <- which doors of THIS device the person's groups cover
    one row per membership <- personnel_access_groups

Nothing is sent from here without an explicit call, and `build_panel_payload`
never touches the network — so a payload can always be inspected first.

The push is **additive**: it writes the records below but never deletes rows that
exist on a panel and not in our database. That keeps a push from locking anybody
out, at the cost of leaving stale users behind. See `agent/README.md` for the
measured per-panel differences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import AccessGroupDoor, Device, Personnel, PersonnelAccessGroup

#: Door numbers a panel can carry, and therefore the width of AuthorizeDoorId.
MAX_DOORS = 4


def door_mask(doors: set[int] | list[int]) -> int:
    """Pack door numbers into the panel's bitmask (door 1 -> bit 0).

    pyzkaccess decodes this same field as four booleans taken from the reversed
    binary form, i.e. door 1 is the least significant bit.
    """
    mask = 0
    for door in doors:
        if 1 <= door <= MAX_DOORS:
            mask |= 1 << (door - 1)
    return mask


def to_zk_date(value: datetime | None) -> int | None:
    """Encode a validity date the way the panels show it (`2020-06-01` -> 20200601).

    INFERRED, NOT YET VERIFIED against a panel: pyzkaccess renders the device's
    raw `StartTime` as a plain date, which fits a YYYYMMDD integer, but nobody has
    round-tripped one on this fleet. Validity windows are NULL for every migrated
    person, so in practice this is currently unused — it only fires if somebody
    sets `valid_from`/`valid_until`.
    """
    if value is None:
        return None
    return int(value.strftime("%Y%m%d"))


@dataclass
class PanelPayload:
    """Everything that would be written to one panel, ready to inspect."""

    device_id: int
    device_name: str
    device_ip: str
    users: list[dict] = field(default_factory=list)
    authorize: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "device_ip": self.device_ip,
            "users": len(self.users),
            "authorize": len(self.authorize),
            "skipped": self.skipped,
        }


def _doors_per_group(db: Session, device_id: int) -> dict[int, set[int]]:
    """Which doors of this device each access group covers."""
    rows = db.scalars(select(AccessGroupDoor).where(AccessGroupDoor.device_id == device_id))
    doors: dict[int, set[int]] = {}
    for link in rows:
        doors.setdefault(link.access_group_id, set()).add(link.door_number)
    return doors


def _people_on_device(db: Session, group_ids: list[int]) -> list[Personnel]:
    """People holding a membership in any group that reaches this device."""
    if not group_ids:
        return []
    return list(
        db.scalars(
            select(Personnel)
            .join(PersonnelAccessGroup, PersonnelAccessGroup.personnel_id == Personnel.id)
            .where(PersonnelAccessGroup.access_group_id.in_(group_ids))
            .options(
                selectinload(Personnel.group_links).selectinload(PersonnelAccessGroup.access_group)
            )
            .order_by(Personnel.employee_id)
            .distinct()
        )
    )


def build_panel_payload(db: Session, device_id: int) -> PanelPayload:
    """Work out the exact records this device needs. Pure computation, no I/O."""
    device = db.get(Device, device_id)
    if device is None:
        raise LookupError(f"Device {device_id} tidak ditemukan")

    payload = PanelPayload(device_id=device.id, device_name=device.name, device_ip=device.ip)
    doors_by_group = _doors_per_group(db, device.id)
    people = _people_on_device(db, list(doors_by_group))

    for person in people:
        pin = (person.employee_id or "").strip()
        if not pin:
            payload.skipped.append(f"personnel {person.id} tanpa badge, dilewati")
            continue

        # `CardNo` is an integer on the panel, so a non-numeric card cannot be
        # sent. Report it rather than writing garbage.
        card = (person.card_number or "").strip()
        record: dict = {"Pin": pin, "Name": person.name or ""}
        if card:
            if card.isdigit():
                record["CardNo"] = int(card)
            else:
                payload.skipped.append(f"{pin}: nomor kartu '{card}' bukan angka, tidak dikirim")
        start = to_zk_date(person.valid_from)
        end = to_zk_date(person.valid_until)
        if start is not None:
            record["StartTime"] = start
        if end is not None:
            record["EndTime"] = end
        payload.users.append(record)

        # One authorization row per group of theirs that reaches this panel.
        for link in person.group_links:
            doors = doors_by_group.get(link.access_group_id)
            if not doors:
                continue
            group = link.access_group
            payload.authorize.append(
                {
                    "Pin": pin,
                    "AuthorizeTimezoneId": group.device_timezone_id if group else 1,
                    "AuthorizeDoorId": door_mask(doors),
                }
            )

    payload.authorize = _dedupe_authorize(payload.authorize)
    return payload


def _dedupe_authorize(rows: list[dict]) -> list[dict]:
    """Drop authorization rows that grant exactly the same thing.

    A person usually holds several access levels, and two of them can easily
    reach the same doors during the same time zone — the migrated data averages
    four levels per person. The panel stores one row per grant, so writing the
    duplicate would be redundant at best. Order is preserved so a dry run reads
    the same way twice.
    """
    seen: set[tuple] = set()
    unique: list[dict] = []
    for row in rows:
        key = (row["Pin"], row["AuthorizeTimezoneId"], row["AuthorizeDoorId"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


# ---------------------------------------------------------------------------
# What actually needs writing
#
# A panel holds hundreds of people. Sending every record to change one of them
# means rewriting rows that were already correct, and each of those is a chance to
# disturb a field this project does not send. So the panel is read first and only
# the differences are written.
# ---------------------------------------------------------------------------
#: Read back before writing: the fields that decide whether a record differs, plus
#: the ones we never send, so their loss can be reported rather than discovered.
#: Measured on the largest panel here (527 users): 9 fields answer in ~90 KB of
#: JSON, comfortably inside the agent's buffer.
PANEL_USER_FIELDS = (
    "Pin",
    "Name",
    "CardNo",
    "Password",
    "Group",
    "StartTime",
    "EndTime",
    "SuperAuthorize",
    "Disable",
)
PANEL_AUTHORIZE_FIELDS = ("Pin", "AuthorizeTimezoneId", "AuthorizeDoorId")

#: Panel fields this project does not set.
#:
#: `SetDeviceData` **replaces** a record instead of patching it, so a field left out
#: of the request is cleared on the panel. Measured on real hardware: rewriting one
#: person to correct their name silently removed the keypad password they had on the
#: panel. These are therefore copied off the panel's own row onto every record we
#: rewrite, which is what makes a rewrite lossless.
UNSENT_USER_FIELDS = (
    "Password",
    "Group",
    "StartTime",
    "EndTime",
    "SuperAuthorize",
    "Disable",
)


def _pin_of(record: dict) -> str:
    return str(record.get("Pin") or "").strip()


def _same_number(left, right) -> bool:
    """Compare a field the panel answers as text with one we hold as a number.

    The panel reports an unset numeric field as `0` or `""`, so both normalise to
    the same empty value — otherwise "no card" would look like a change from "no
    card" and rewrite a record for nothing.
    """

    def normalise(value) -> str:
        text = "" if value is None else str(value).strip()
        return "" if text == "0" else text

    return normalise(left) == normalise(right)


def _carry_over(record: dict, current: dict) -> dict:
    """Add the panel's own values for the fields this project does not set.

    Without this a rewrite is destructive: the panel replaces rather than patches,
    so omitting a field deletes it.
    """
    merged = dict(record)
    for name in UNSENT_USER_FIELDS:
        if name in merged:
            continue
        value = current.get(name)
        if value is None or str(value).strip() == "":
            continue
        merged[name] = value
    return merged


def _holds_extra_data(row: dict) -> bool:
    """Does the panel's copy hold anything in a field we would otherwise drop?"""
    return any(str(row.get(name) or "").strip() not in ("", "0") for name in UNSENT_USER_FIELDS)


def _user_matches(record: dict, current: dict) -> bool:
    """Do the fields this project manages already match the panel's copy?"""
    if str(current.get("Name") or "").strip() != str(record.get("Name") or "").strip():
        return False
    if not _same_number(current.get("CardNo"), record.get("CardNo")):
        return False
    # Validity dates are only ever sent when they are set, so only compare them
    # when this record carries one.
    for name in ("StartTime", "EndTime"):
        if name in record and not _same_number(current.get(name), record[name]):
            return False
    return True


def plan_user_writes(desired: list[dict], existing: list[dict]) -> tuple[list[dict], dict]:
    """Return only the `user` records that differ from the panel. Pure, no I/O.

    A rewritten record carries over the panel's values for the fields this project
    does not set, so writing one person again cannot cost them a keypad password.
    `carried_over` lists the Pins where that actually happened, so an operator can
    see which records depend on it.
    """
    by_pin = {_pin_of(row): row for row in existing}
    to_write: list[dict] = []
    carried_over: list[str] = []
    created = updated = unchanged = 0

    for record in desired:
        pin = _pin_of(record)
        current = by_pin.get(pin)

        if current is None:
            to_write.append(record)
            created += 1
            continue

        if _user_matches(record, current):
            unchanged += 1
            continue

        to_write.append(_carry_over(record, current))
        updated += 1
        if _holds_extra_data(current):
            carried_over.append(pin)

    return to_write, {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "carried_over": carried_over,
    }


def _authorize_key(row: dict) -> tuple[str, str]:
    """The grant a single authorization row represents: (time zone, door mask)."""
    return (
        str(row.get("AuthorizeTimezoneId") or "").strip(),
        str(row.get("AuthorizeDoorId") or "").strip(),
    )


def _rows_by_pin(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(_pin_of(row), []).append(row)
    return grouped


def plan_authorize_writes(
    desired: list[dict],
    existing: list[dict],
    *,
    revoke_pins: set[str] | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Work out which `userauthorize` rows to add, and which to remove. Pure, no I/O.

    Writing alone is not enough. A push that only ever adds leaves the old rights in
    place, so taking somebody out of an access level — or moving them to another one
    — silently keeps every door they used to open. Measured: a person moved from
    `TEKNISI/IT` to `AKSES UMUM KARYAWAN` still had `AuthorizeDoorId 1` on RUANGAN
    SERVER afterwards, and the door still opened.

    `revoke_pins` names the people this call may *remove* rights for, and it is
    deliberately narrow: only an explicitly named person is treated as authoritative.
    A panel can hold people this database has never heard of — OK PETUGAS carries 405
    users against the 361 we can account for — and revoking those would lock out
    strangers. `None` means additive-only, which is what a whole-panel push does.
    """
    on_panel = _rows_by_pin(existing)
    desired_by_pin = _rows_by_pin(desired)

    to_write: list[dict] = []
    to_delete: list[dict] = []
    unchanged = updated = 0

    for pin, rows in desired_by_pin.items():
        current_rows = on_panel.get(pin, [])
        if _rights_of(current_rows) == _rights_of(rows):
            unchanged += 1
            continue

        to_write.extend(rows)
        updated += 1
        if revoke_pins is not None and pin in revoke_pins:
            granted = _rights_of(rows)
            to_delete.extend(row for row in current_rows if _authorize_key(row) not in granted)

    # Somebody who should hold no rights here at all — typically because they were
    # removed from the level that reached this panel. Their rows have to go, or the
    # removal never reaches the panel.
    revoked = 0
    for pin in sorted(revoke_pins or ()):
        if pin in desired_by_pin:
            continue
        stale = on_panel.get(pin, [])
        if stale:
            to_delete.extend(stale)
            revoked += 1

    return (
        to_write,
        to_delete,
        {
            "updated": updated,
            "unchanged": unchanged,
            "revoked": revoked,
        },
    )


def _rights_of(rows: list[dict]) -> set[tuple[str, str]]:
    """The (time zone, door mask) pairs a set of rows grants."""
    return {_authorize_key(row) for row in rows}
