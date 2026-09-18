"""Personnel master data and device synchronisation."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import AccessGroup, AccessGroupDoor, Device, Personnel, PersonnelAccessGroup
from app.schemas import PersonnelCreate, PersonnelUpdate
from app.services.access_group_service import AccessGroupNotFoundError
from app.services.department_service import DepartmentNotFoundError, DepartmentService
from app.services.device_service import DeviceService
from app.services.panel_push import (
    PANEL_AUTHORIZE_FIELDS,
    PANEL_USER_FIELDS,
    build_panel_payload,
    plan_authorize_writes,
    plan_user_writes,
)
from app.services.push_agent import PushAgentClient, PushAgentError, PushAgentNotConfigured

logger = logging.getLogger(__name__)


class PersonnelNotFoundError(LookupError):
    pass


class DuplicatePersonnelError(ValueError):
    pass


#: Columns that are nullable but UNIQUE. A stray empty string would count as a
#: real value and collide with the next person saved without one, so blanks are
#: normalised to NULL before they ever reach the database.
_BLANKABLE = ("card_number", "pin")


def _normalise(data: dict) -> dict:
    """Trim text fields and turn empty/whitespace cards & PINs into NULL."""
    for field in ("employee_id", "name"):
        value = data.get(field)
        if isinstance(value, str):
            data[field] = value.strip()
    for field in _BLANKABLE:
        if field in data:
            value = data[field]
            data[field] = value.strip() or None if isinstance(value, str) else value
    return data


def _with_relations():
    """Eager-load the relationships the API exposes, to avoid N+1 queries."""
    return (
        selectinload(Personnel.group_links).selectinload(PersonnelAccessGroup.access_group),
        selectinload(Personnel.department_ref),
    )


class PersonnelService:
    def __init__(self, db: Session):
        self.db = db
        self.devices = DeviceService(db)
        self.departments = DepartmentService(db)

    # -- CRUD --------------------------------------------------------------
    def list(self, *, active_only: bool = False, q: str | None = None) -> list[Personnel]:
        stmt = select(Personnel).options(*_with_relations()).order_by(Personnel.employee_id)
        if active_only:
            stmt = stmt.where(Personnel.is_active.is_(True))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Personnel.name.ilike(like) | Personnel.employee_id.ilike(like))
        return list(self.db.scalars(stmt))

    def get(self, personnel_id: int) -> Personnel:
        obj = self.db.scalars(
            select(Personnel)
            .options(*_with_relations())
            .where(Personnel.id == personnel_id)
            .execution_options(populate_existing=True)
        ).first()
        if obj is None:
            raise PersonnelNotFoundError(f"Personnel {personnel_id} tidak ditemukan")
        return obj

    def create(self, payload: PersonnelCreate) -> Personnel:
        data = _normalise(payload.model_dump())
        # Membership and department live in their own tables, so those fields
        # are pulled out and resolved before anything is written.
        single_group = data.pop("access_group_id", None)
        group_ids = data.pop("access_group_ids", None)
        department_name = data.pop("department", None)
        department_id = data.pop("department_id", None)

        # Validate the groups BEFORE writing anything: a bad group id must not
        # leave a half-created person behind (the request would report 404 while
        # the row quietly existed).
        wanted = self._validate_groups(group_ids, single_group)
        self._assert_unique(data.get("employee_id"), data.get("card_number"))
        data["department_id"] = self._resolve_department(department_id, department_name)

        obj = Personnel(**data)
        self.db.add(obj)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicatePersonnelError(
                "employee_id atau card_number sudah dipakai personel lain"
            ) from exc

        for group_id in wanted:
            self.db.add(PersonnelAccessGroup(personnel_id=obj.id, access_group_id=group_id))
        if wanted:
            obj.access_group_id = self._legacy_hint(wanted)

        self.db.commit()
        return self.get(obj.id)

    def update(self, personnel_id: int, payload: PersonnelUpdate) -> Personnel:
        obj = self.get(personnel_id)
        data = _normalise(payload.model_dump(exclude_unset=True))
        single_group = data.pop("access_group_id", None)
        group_ids = data.pop("access_group_ids", None)
        department_name = data.pop("department", None)
        department_id = data.pop("department_id", None)
        clear_department = data.pop("clear_department", False)

        # Work out the target membership set first, so validation failures abort
        # before any field is touched.
        target: set[int] | None = None
        if group_ids is not None:
            target = self._validate_groups(group_ids, None)
        if single_group is not None:
            self._validate_groups(None, single_group)
            current = {link.access_group_id for link in obj.group_links}
            target = (target if target is not None else current) | {single_group}

        # The badge is mandatory, so a null here means "leave it alone" rather
        # than "erase it" — erasing would violate NOT NULL.
        if data.get("employee_id") is None:
            data.pop("employee_id", None)

        # Guard both identifiers against the values that will actually be
        # stored, skipping this person's own row so re-saving is a no-op.
        if "employee_id" in data or "card_number" in data:
            self._assert_unique(
                data.get("employee_id", obj.employee_id),
                data.get("card_number", obj.card_number),
                exclude_id=obj.id,
            )

        if clear_department:
            data["department_id"] = None
        elif department_id is not None or department_name is not None:
            data["department_id"] = self._resolve_department(department_id, department_name)

        for field, value in data.items():
            setattr(obj, field, value)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicatePersonnelError(
                "employee_id atau card_number sudah dipakai personel lain"
            ) from exc

        if target is not None:
            current = {link.access_group_id for link in obj.group_links}
            for group_id in target - current:
                self.db.add(PersonnelAccessGroup(personnel_id=obj.id, access_group_id=group_id))
            for link in list(obj.group_links):
                if link.access_group_id not in target:
                    self.db.delete(link)
            obj.access_group_id = self._legacy_hint(target)

        self.db.commit()
        return self.get(personnel_id)

    def _resolve_department(
        self, department_id: int | None, department_name: str | None
    ) -> int | None:
        """Return the department id to store, creating the record if needed.

        An id is taken as-is (404 when unknown); a name is matched against the
        master and created on first use, which keeps the ZKAccess importer and
        simple API clients working.
        """
        if department_id is not None:
            try:
                return self.departments.get(department_id).id
            except DepartmentNotFoundError as exc:
                raise DepartmentNotFoundError(str(exc)) from exc

        if department_name is None:
            return None
        department = self.departments.resolve_or_create(department_name)
        return department.id if department is not None else None

    @staticmethod
    def _legacy_hint(group_ids: set[int]) -> int | None:
        """Value for the back-compat `Personnel.access_group_id` column.

        It can only hold one group, so it is only meaningful when exactly one is
        set. Anything else becomes NULL rather than an arbitrary pick.
        """
        return next(iter(group_ids)) if len(group_ids) == 1 else None

    def _assert_unique(
        self,
        employee_id: str | None,
        card_number: str | None,
        *,
        exclude_id: int | None = None,
    ) -> None:
        """Refuse to re-issue a badge or a card that already belongs to somebody.

        Two people sharing one card would both open the same doors, so the card
        holder is part of the access decision. The database has UNIQUE
        constraints on both columns as a backstop, but a raw IntegrityError
        cannot say *which* value clashed or who holds it — this check names
        both, because that is what lets an operator fix the input.
        """
        clashes: list[str] = []
        checks = (
            (Personnel.employee_id, "Badge/PIN", employee_id),
            (Personnel.card_number, "No. kartu", card_number),
        )
        for column, label, value in checks:
            if not value:
                continue
            stmt = select(Personnel).where(column == value)
            if exclude_id is not None:
                stmt = stmt.where(Personnel.id != exclude_id)
            holder = self.db.scalars(stmt).first()
            if holder is not None:
                clashes.append(f"{label} {value} sudah dipakai {holder.name}")

        if clashes:
            raise DuplicatePersonnelError("; ".join(clashes) + ". Kartu tidak boleh dobel.")

    def _validate_groups(self, group_ids: list[int] | None, single_group: int | None) -> set[int]:
        """Return the requested group ids after checking they all exist."""
        wanted = set(group_ids or [])
        if single_group is not None:
            wanted.add(single_group)
        if not wanted:
            return set()

        # Flush pending changes first so the lookup cannot return stale data.
        self.db.flush()
        valid = set(self.db.scalars(select(AccessGroup.id).where(AccessGroup.id.in_(wanted))).all())
        unknown = wanted - valid
        if unknown:
            raise AccessGroupNotFoundError(f"Access level {sorted(unknown)} tidak ditemukan")
        return valid

    def delete(self, personnel_id: int) -> None:
        obj = self.get(personnel_id)
        self.db.delete(obj)
        self.db.commit()

    def count(self) -> int:
        return int(self.db.scalar(select(func.count()).select_from(Personnel)) or 0)

    # -- device <-> central -------------------------------------------------
    def count_on_device(self, device_id: int) -> int:
        """Actual personnel count stored on a panel."""
        device = self.devices.get(device_id)
        with self.devices.client_for(device) as client:
            return client.count_personnel()

    def pull_from_device(self, device_id: int, *, fields: list[str] | None = None) -> list[dict]:
        """Read the users that already exist on a panel.

        Used to bootstrap panels that were configured before this system
        existed. Read-only: nothing is written to the device.
        """
        device = self.devices.get(device_id)
        with self.devices.client_for(device) as client:
            records = client.list_personnel(fields)
        device.personnel_count = len(records)
        self.db.commit()
        return records

    def import_from_device(self, device_id: int, *, overwrite: bool = False) -> dict:
        """Import device users into the central personnel table."""
        records = self.pull_from_device(device_id)
        created = updated = skipped = 0

        for rec in records:
            employee_id = _text(rec.get("UID") or rec.get("Pin"))
            if not employee_id:
                skipped += 1
                continue
            existing = self.db.scalars(
                select(Personnel).where(Personnel.employee_id == employee_id)
            ).first()

            card_number = _text(rec.get("CardNo"))
            name = _text(rec.get("Name")) or f"User {employee_id}"

            if existing is None:
                self.db.add(
                    Personnel(
                        employee_id=employee_id,
                        name=name,
                        card_number=card_number,
                        pin=_text(rec.get("Pin")),
                        device_id=device_id,
                        is_active=True,
                    )
                )
                created += 1
            elif overwrite:
                existing.card_number = card_number or existing.card_number
                existing.name = name or existing.name
                existing.pin = _text(rec.get("Pin")) or existing.pin
                updated += 1
            else:
                skipped += 1

        self.db.commit()
        return {
            "device_id": device_id,
            "device_records": len(records),
            "created": created,
            "updated": updated,
            "skipped": skipped,
        }

    def push_to_device(
        self,
        device_id: int,
        personnel_ids: list[int] | None = None,
        *,
        dry_run: bool = False,
    ) -> dict:
        """Write central personnel data down into one panel.

        The write itself happens in a Windows agent, because the official
        `plcommpro.dll` is 32-bit and this backend runs 64-bit Python on Linux.
        See `app.services.panel_push` for how the records are derived and
        `agent/zk_push_agent.py` for the other half.

        `dry_run` returns exactly what *would* be written without sending it —
        the safe way to look before touching a live panel.
        """
        device = self.devices.get(device_id)  # 404 when the device is gone
        payload = build_panel_payload(self.db, device_id)

        #: The people this call is authoritative about. Only an explicitly named
        #: person may have rights *removed* — a whole-panel push must stay additive,
        #: because panels hold users this database cannot account for.
        revoke_pins: set[str] | None = None
        if personnel_ids is not None:
            pins = {
                (row or "").strip()
                for row in self.db.scalars(
                    select(Personnel.employee_id).where(Personnel.id.in_(personnel_ids))
                )
            }
            revoke_pins = pins
            payload.users = [r for r in payload.users if r["Pin"] in pins]
            payload.authorize = [r for r in payload.authorize if r["Pin"] in pins]

        result: dict = {"device": payload.summary()}
        if dry_run:
            result["dry_run"] = True
            result["sample_users"] = payload.users[:5]
            result["sample_authorize"] = payload.authorize[:5]
            return result

        agent = PushAgentClient()
        if not agent.configured:
            raise PushAgentNotConfigured(
                f"Push ke '{device.name}' ({device.ip}) belum dikonfigurasi. "
                "Jalankan agent/zk_push_agent.py di server Windows, lalu set "
                "PUSH_AGENT_URL (dan PUSH_AGENT_TOKEN) di backend."
            )

        # Read the panel before writing to it. Two reasons.
        #
        # First, only a record that actually differs needs rewriting. Comparing
        # turns "rewrite five hundred people to change one" into "write the one that
        # changed", which is also the only way to keep from disturbing rows that
        # were already correct — and the panel holds fields this project never
        # sends.
        #
        # Second, a panel we cannot read is a panel we must not blind-write: falling
        # back to "send everything" would do exactly what comparing exists to
        # prevent.
        try:
            # `user` is only needed when this call has somebody to place here. A
            # panel the person no longer reaches has nothing to add, and skipping
            # that read keeps a sweep of every panel affordable.
            current_users = (
                agent.read_table(device.ip, "user", list(PANEL_USER_FIELDS))
                if payload.users
                else []
            )
            current_authorize = agent.read_table(
                device.ip, "userauthorize", list(PANEL_AUTHORIZE_FIELDS)
            )
        except PushAgentError as exc:
            raise PushAgentError(
                f"{device.name} ({device.ip}) tidak bisa dibaca, jadi tidak ada yang "
                "ditulis — memaksa kirim akan menulis ulang semua user di panel itu. "
                f"{exc}"
            ) from exc

        users_to_write, user_plan = plan_user_writes(payload.users, current_users)
        rights_to_write, rights_to_delete, rights_plan = plan_authorize_writes(
            payload.authorize, current_authorize, revoke_pins=revoke_pins
        )

        # `user` first: an authorization row whose Pin is unknown is useless.
        if users_to_write:
            agent.write_table(device.ip, "user", users_to_write)
        if rights_to_write:
            agent.write_table(device.ip, "userauthorize", rights_to_write)
        # Removing rights is what makes a removal from an access level real: without
        # it the old row survives and the door still opens. The `user` row is
        # deliberately left in place — deleting and re-adding it would cost the
        # fields this project does not send (see `UNSENT_USER_FIELDS`), and access is
        # granted by `userauthorize`, not by being known to the panel.
        if rights_to_delete:
            agent.delete_table(device.ip, "userauthorize", rights_to_delete)

        result["written"] = {
            "user": len(users_to_write),
            "userauthorize": len(rights_to_write),
        }
        result["deleted"] = {"userauthorize": len(rights_to_delete)}
        result["unchanged"] = {
            "user": user_plan["unchanged"],
            "userauthorize": rights_plan["unchanged"],
        }
        result["plan"] = {"user": user_plan, "userauthorize": rights_plan}

        # A record that had panel-only data carried across is worth naming: it is
        # the proof that rewriting that person did not cost them anything.
        if user_plan["carried_over"]:
            result["note"] = (
                f"{len(user_plan['carried_over'])} record di {device.name} ditulis ulang "
                "sambil membawa data yang tidak dikelola aplikasi ini (Password/Group/"
                "SuperAuthorize/Disable) dari panel: " + ", ".join(user_plan["carried_over"][:10])
            )
        return result

    def devices_for_personnel(self, personnel_ids: list[int]) -> list[Device]:
        """Every panel the given people's access levels reach.

        Granting somebody an access level grants every door in that level, so the
        panels that need their Pin are *all* the devices those levels cover — not
        just the one the operator happened to be looking at.
        """
        if not personnel_ids:
            return []
        return list(
            self.db.scalars(
                select(Device)
                .join(AccessGroupDoor, AccessGroupDoor.device_id == Device.id)
                .join(
                    PersonnelAccessGroup,
                    PersonnelAccessGroup.access_group_id == AccessGroupDoor.access_group_id,
                )
                .where(PersonnelAccessGroup.personnel_id.in_(personnel_ids))
                .order_by(Device.name)
                .distinct()
            )
        )

    def assert_push_ready(self, personnel_ids: list[int], *, dry_run: bool = False) -> list[Device]:
        """The panels these people belong on, refusing an unconfigured agent.

        Nothing to write means nothing to configure: somebody with no access level
        reaches no door, and answering 503 there would send the operator off to
        set up a push agent when the real answer is "this person has no access
        level yet".
        """
        covered = self.devices_for_personnel(personnel_ids)
        if covered and not dry_run and not PushAgentClient().configured:
            raise PushAgentNotConfigured(
                "Push belum dikonfigurasi. Jalankan agent/zk_push_agent.py di server "
                "Windows, lalu set PUSH_AGENT_URL (dan PUSH_AGENT_TOKEN) di backend."
            )
        return covered

    def iter_push_to_all_devices(
        self,
        personnel_ids: list[int],
        *,
        dry_run: bool = False,
        device_ids: list[int] | None = None,
    ) -> Iterator[dict]:
        """Walk the panels one at a time, reporting each result as it lands.

        It walks panels the person is *no longer* covered by as well, which is the
        part that matters when access is taken away. Moving somebody from one
        access level to another used to leave their old rights on every panel of
        the level they left — the door still opened — because the sweep only ever
        looked at where they now belong, and a push that only writes never removes.

        `device_ids` narrows the walk to just those panels. Granting a level to
        somebody is about *that level's* panels, and reporting the other nineteen
        as "tidak memuat orang ini" buries the ones that matter. The wide default
        stays for the per-person sweep, which has to consult every panel because
        that is exactly where a stale grant hides.

        A whole sweep takes a while, so each panel is yielded the moment it
        answers: the dashboard shows the operator which panels are already written
        instead of a spinner that says nothing. The summary is always the last
        event, even when panels failed, so a sweep that went badly can still say
        how far it got.
        """
        covered = self.assert_push_ready(personnel_ids, dry_run=dry_run)
        covered_ids = {device.id for device in covered}

        # Every panel, not just the covered ones: a stale authorization row on a
        # panel the person has left is exactly what this has to find and remove.
        query = select(Device).order_by(Device.name)
        if device_ids is not None:
            query = query.where(Device.id.in_(device_ids))
        devices = list(self.db.scalars(query))

        results: list[dict] = []
        for device in devices:
            base = {
                "device_id": device.id,
                "device_name": device.name,
                "device_ip": device.ip,
                "covered": device.id in covered_ids,
            }
            try:
                summary = self.push_to_device(device.id, personnel_ids, dry_run=dry_run)
                row = {
                    **base,
                    "ok": True,
                    "written": summary.get("written"),
                    "deleted": summary.get("deleted"),
                    "payload": summary.get("device"),
                }
            except (PushAgentNotConfigured, PushAgentError) as exc:
                # One unreachable panel must not abort the rest: the operator needs
                # to see exactly which panel still needs attention.
                row = {**base, "ok": False, "error": str(exc)}
            results.append(row)
            yield {"type": "panel", **row}

        def _total(key: str) -> int:
            return sum((row.get(key) or {}).get("userauthorize", 0) for row in results)

        yield {
            "type": "summary",
            "personnel_ids": personnel_ids,
            "device_count": len(devices),
            "covered_count": sum(1 for device in devices if device.id in covered_ids),
            "succeeded": sum(1 for row in results if row["ok"]),
            "failed": sum(1 for row in results if not row["ok"]),
            "written_authorize": _total("written"),
            "deleted_authorize": _total("deleted"),
            "dry_run": dry_run,
        }

    def push_to_all_devices(
        self,
        personnel_ids: list[int],
        *,
        dry_run: bool = False,
        device_ids: list[int] | None = None,
    ) -> dict:
        """Push these people into every panel that should know about them.

        `push_to_device` only refreshes the single panel it is aimed at, so it is
        easy to grant somebody an access level spanning twenty panels and leave
        nineteen of them stale. This is the whole set in one payload, for callers
        that do not want to watch the sweep happen; the panels themselves are
        walked by `iter_push_to_all_devices`, which also takes `device_ids`.
        """
        results: list[dict] = []
        summary: dict = {}
        events = self.iter_push_to_all_devices(
            personnel_ids, dry_run=dry_run, device_ids=device_ids
        )
        for event in events:
            if event["type"] == "summary":
                summary = {key: value for key, value in event.items() if key != "type"}
            else:
                results.append({key: value for key, value in event.items() if key != "type"})
        return {**summary, "results": results}


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
