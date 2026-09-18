"""Pushing our data into a panel.

Two halves, tested separately:

1. `app.services.panel_push` — turns our tables into the records a panel stores.
   Pure computation, so it is tested directly.
2. `app.services.push_agent` — the HTTP contract with the Windows agent that
   actually performs the write (the official 32-bit DLL cannot be loaded by this
   64-bit backend). Tested against a stubbed transport.

The wire format the agent finally sends is locked down too, because getting it
wrong would corrupt a live panel rather than fail loudly.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.panel_push import (
    build_panel_payload,
    door_mask,
    plan_authorize_writes,
    plan_user_writes,
    to_zk_date,
)
from app.services.push_agent import PushAgentClient, PushAgentError, PushAgentNotConfigured


# -- the mask the panel stores ---------------------------------------------
def test_door_mask_matches_the_panel_bit_layout():
    """`AuthorizeDoorId` is a 4-door bitmask with door 1 as the lowest bit."""
    assert door_mask([]) == 0
    assert door_mask([1]) == 0b0001
    assert door_mask([1, 2]) == 0b0011
    assert door_mask([2, 4]) == 0b1010
    assert door_mask([3, 1]) == 0b0101


def test_door_mask_ignores_numbers_the_panel_cannot_have():
    """A panel has four doors; anything else must not shift past the mask."""
    assert door_mask([0, 5, 99]) == 0


def test_zk_date_encodes_as_yyyymmdd():
    from datetime import datetime

    assert to_zk_date(None) is None
    assert to_zk_date(datetime(2020, 6, 1)) == 20200601


# -- deriving a panel's records --------------------------------------------
def _seed_panel(db, make_device, **person_extra):
    """One panel with doors 1+2, one access level using timezone 7, one person."""
    from app.models import AccessGroup, AccessGroupDoor, Personnel, PersonnelAccessGroup

    device = make_device(ip="10.100.1.14")
    group = AccessGroup(name="ICU", device_timezone_id=7)
    db.add(group)
    db.flush()
    db.add_all(
        [
            AccessGroupDoor(access_group_id=group.id, device_id=device.id, door_number=1),
            AccessGroupDoor(access_group_id=group.id, device_id=device.id, door_number=2),
        ]
    )
    person = Personnel(employee_id="1001", name="Budi", card_number="5001", **person_extra)
    db.add(person)
    db.flush()
    db.add(PersonnelAccessGroup(personnel_id=person.id, access_group_id=group.id))
    db.commit()
    return device, group, person


def test_build_panel_payload_maps_person_and_access_rights(db, make_device):
    device, _, _ = _seed_panel(db, make_device)

    payload = build_panel_payload(db, device.id)

    assert payload.users == [{"Pin": "1001", "Name": "Budi", "CardNo": 5001}]
    assert payload.authorize == [
        {"Pin": "1001", "AuthorizeTimezoneId": 7, "AuthorizeDoorId": 0b0011}
    ]
    assert payload.skipped == []


def test_build_panel_payload_only_covers_the_doors_of_that_panel(db, make_device):
    """A level with no door on this panel must not put the person on it."""
    _seed_panel(db, make_device)
    other_panel = make_device(ip="10.100.1.15")

    payload = build_panel_payload(db, other_panel.id)

    assert payload.users == []
    assert payload.authorize == []


def test_build_panel_payload_reports_a_card_it_cannot_send(db, make_device):
    """`CardNo` is an integer on the panel, so a non-numeric card is skipped loudly."""
    device, _, person = _seed_panel(db, make_device)
    person.card_number = "ABCD1234"
    db.commit()

    payload = build_panel_payload(db, device.id)

    assert "CardNo" not in payload.users[0]
    assert payload.users[0]["Pin"] == "1001"
    assert any("ABCD1234" in note for note in payload.skipped)


def test_build_panel_payload_reports_a_person_without_a_badge(db, make_device):
    """The Pin is the panel's only handle on a person, so it cannot be blank."""
    device, _, person = _seed_panel(db, make_device)
    person.employee_id = "   "
    db.commit()

    payload = build_panel_payload(db, device.id)

    assert payload.users == []
    assert any("tanpa badge" in note for note in payload.skipped)


def test_two_levels_granting_the_same_thing_write_one_row(db, make_device):
    """A person averages four access levels; duplicates must not be written.

    `userauthorize` stores one row per grant, so two levels that reach the same
    doors in the same time zone would produce a redundant row.
    """
    from app.models import AccessGroup, AccessGroupDoor, PersonnelAccessGroup

    device, group, person = _seed_panel(db, make_device)
    twin = AccessGroup(name="ICU twin", device_timezone_id=group.device_timezone_id)
    db.add(twin)
    db.flush()
    # Same doors, same timezone as the level the person already holds.
    db.add_all(
        [
            AccessGroupDoor(access_group_id=twin.id, device_id=device.id, door_number=1),
            AccessGroupDoor(access_group_id=twin.id, device_id=device.id, door_number=2),
        ]
    )
    db.add(PersonnelAccessGroup(personnel_id=person.id, access_group_id=twin.id))
    db.commit()

    payload = build_panel_payload(db, device.id)

    assert payload.authorize == [
        {"Pin": "1001", "AuthorizeTimezoneId": 7, "AuthorizeDoorId": 0b0011}
    ]


def test_two_levels_granting_different_doors_keep_both_rows(db, make_device):
    """Deduplication must not swallow a genuinely different grant."""
    from app.models import AccessGroup, AccessGroupDoor, PersonnelAccessGroup

    device, group, person = _seed_panel(db, make_device)
    other = AccessGroup(name="Server", device_timezone_id=9)
    db.add(other)
    db.flush()
    db.add(AccessGroupDoor(access_group_id=other.id, device_id=device.id, door_number=3))
    db.add(PersonnelAccessGroup(personnel_id=person.id, access_group_id=other.id))
    db.commit()

    payload = build_panel_payload(db, device.id)

    assert payload.authorize == [
        {"Pin": "1001", "AuthorizeTimezoneId": 7, "AuthorizeDoorId": 0b0011},
        {"Pin": "1001", "AuthorizeTimezoneId": 9, "AuthorizeDoorId": 0b0100},
    ]


# -- the HTTP contract with the Windows agent ------------------------------
def _stub_transport(monkeypatch, status=200, payload=None):
    captured = {}

    class FakeResponse:
        status_code = status
        content = b"{}"
        text = ""

        def json(self):
            return payload or {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(httpx, "request", fake_request)
    return captured


def test_push_agent_client_sends_records_with_the_token(monkeypatch):
    captured = _stub_transport(monkeypatch, payload={"written": 1})

    client = PushAgentClient("http://win-agent:8081/", token="s3cret")
    result = client.write_table("10.100.1.14", "user", [{"Pin": "1001", "CardNo": 5001}])

    assert result == {"written": 1}
    assert captured["method"] == "POST"
    # A trailing slash in the configured URL must not double up.
    assert captured["url"] == "http://win-agent:8081/panels/10.100.1.14/tables/user"
    assert captured["headers"]["Authorization"] == "Bearer s3cret"
    assert captured["json"]["records"] == [{"Pin": "1001", "CardNo": 5001}]


def test_push_agent_client_asks_the_agent_to_delete(monkeypatch):
    captured = _stub_transport(monkeypatch, payload={"deleted": 1})

    PushAgentClient("http://win-agent:8081", token="t").delete_table(
        "10.100.1.14", "userauthorize", [{"Pin": "1001"}]
    )

    assert captured["json"] == {"records": [{"Pin": "1001"}], "delete": True}


def test_push_agent_client_surfaces_the_agents_error_text(monkeypatch):
    _stub_transport(monkeypatch, status=502, payload={"error": "Tidak bisa konek ke panel"})

    with pytest.raises(PushAgentError, match="Tidak bisa konek ke panel"):
        PushAgentClient("http://win-agent:8081", token="t").health()


def test_push_agent_without_a_url_says_so(monkeypatch):
    client = PushAgentClient(base_url="", token="")

    assert client.configured is False
    with pytest.raises(PushAgentNotConfigured, match="PUSH_AGENT_URL"):
        client.read_table("10.100.1.14", "user")


def test_an_explicitly_empty_url_overrides_a_configured_default(monkeypatch):
    """`base_url=""` must mean "no agent", not "fall back to the setting".

    Otherwise a caller cannot opt out, which matters because every push failure
    path is exercised with the agent deliberately disabled.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "push_agent_url", "http://somewhere:8081")

    assert PushAgentClient().configured is True
    assert PushAgentClient(base_url="").configured is False


# -- writing only what changed ---------------------------------------------
def _panel_user(**overrides) -> dict:
    """A row as the panel reports it: every value arrives as text."""
    row = {
        "Pin": "1001",
        "Name": "Budi",
        "CardNo": "5001",
        "Password": "",
        "Group": "0",
        "StartTime": "0",
        "EndTime": "0",
        "SuperAuthorize": "0",
        "Disable": "0",
    }
    row.update(overrides)
    return row


def test_a_person_already_correct_on_the_panel_is_not_rewritten():
    """The whole point of comparing: do not touch five hundred rows to change one."""
    desired = [{"Pin": "1001", "Name": "Budi", "CardNo": 5001}]

    to_write, plan = plan_user_writes(desired, [_panel_user()])

    assert to_write == []
    assert plan["unchanged"] == 1
    assert plan["created"] == 0


def test_a_person_the_panel_does_not_have_is_created():
    desired = [{"Pin": "2150141426", "Name": "tes", "CardNo": 2150141426}]

    to_write, plan = plan_user_writes(desired, [_panel_user()])

    assert to_write == desired
    assert plan["created"] == 1
    assert plan["carried_over"] == []


def test_a_changed_name_is_written():
    desired = [{"Pin": "1001", "Name": "Budi Santoso", "CardNo": 5001}]

    to_write, plan = plan_user_writes(desired, [_panel_user()])

    assert [row["Pin"] for row in to_write] == ["1001"]
    assert to_write[0]["Name"] == "Budi Santoso"
    assert plan["updated"] == 1


def test_a_changed_card_is_written():
    desired = [{"Pin": "1001", "Name": "Budi", "CardNo": 9999}]

    to_write, plan = plan_user_writes(desired, [_panel_user()])

    assert [row["Pin"] for row in to_write] == ["1001"]
    assert to_write[0]["CardNo"] == 9999
    assert plan["updated"] == 1


def test_the_panels_zeros_do_not_look_like_a_change():
    """The panel answers "0" for an unset number.

    Without normalising that, somebody with no card would look changed on every
    single push and be rewritten forever.
    """
    desired = [{"Pin": "1001", "Name": "Budi"}]

    to_write, _ = plan_user_writes(desired, [_panel_user(CardNo="0")])

    assert to_write == []


def test_an_untouched_person_keeps_the_password_the_panel_holds():
    """The safety property this exists for.

    `Fega` carries a keypad password on RUANGAN SERVER that this project never
    sends. As long as nothing about him changed he must not be rewritten at all,
    and that is what keeps the password safe.
    """
    desired = [{"Pin": "101138154", "Name": "Fega", "CardNo": 1011381546}]
    panel = [_panel_user(Pin="101138154", Name="Fega", CardNo="1011381546", Password="123456")]

    to_write, plan = plan_user_writes(desired, panel)

    assert to_write == []
    assert plan["unchanged"] == 1


def test_a_needed_rewrite_carries_the_password_back_so_it_survives():
    """The bug this exists for, measured on real hardware.

    `SetDeviceData` replaces a record rather than patching it, so a rewrite that
    omits a field clears it. Correcting one person's name on RUANGAN SERVER wiped
    the keypad password they had there — the panel went from `Password=123456` to
    empty. Every field we do not manage is therefore copied off the panel's own row
    onto the record we are about to write.
    """
    desired = [{"Pin": "101138154", "Name": "Fega Suseno", "CardNo": 1011381546}]
    panel = [_panel_user(Pin="101138154", Name="Fega", CardNo="1011381546", Password="123456")]

    to_write, plan = plan_user_writes(desired, panel)

    assert to_write == [
        {
            "Pin": "101138154",
            "Name": "Fega Suseno",
            "CardNo": 1011381546,
            "Password": "123456",
            "Group": "0",
            "StartTime": "0",
            "EndTime": "0",
            "SuperAuthorize": "0",
            "Disable": "0",
        }
    ]
    assert plan["carried_over"] == ["101138154"]


def test_carry_over_never_overrides_what_this_project_manages():
    """A field we do send wins, otherwise the panel could hold a stale card forever."""
    desired = [{"Pin": "1001", "Name": "Budi Baru", "CardNo": 7777}]
    panel = [_panel_user(Name="Budi", CardNo="5001", Password="123456")]

    to_write, _ = plan_user_writes(desired, panel)

    assert to_write[0]["Name"] == "Budi Baru"
    assert to_write[0]["CardNo"] == 7777
    assert to_write[0]["Password"] == "123456"


def test_only_the_changed_records_are_returned():
    """Five hundred people, one of them renamed — one record on the wire."""
    desired = [{"Pin": f"{1000 + i}", "Name": f"User {i}"} for i in range(500)]
    panel = [_panel_user(Pin=f"{1000 + i}", Name=f"User {i}", CardNo="0") for i in range(500)]
    desired[317] = {"Pin": "1317", "Name": "Nama Baru"}

    to_write, plan = plan_user_writes(desired, panel)

    assert [row["Pin"] for row in to_write] == ["1317"]
    assert plan == {"created": 0, "updated": 1, "unchanged": 499, "carried_over": []}


# -- access rights ---------------------------------------------------------
def test_rights_that_already_match_are_not_rewritten():
    desired = [{"Pin": "1001", "AuthorizeTimezoneId": 1, "AuthorizeDoorId": 1}]
    panel = [{"Pin": "1001", "AuthorizeTimezoneId": "1", "AuthorizeDoorId": "1"}]

    to_write, to_delete, plan = plan_authorize_writes(desired, panel)

    assert to_write == []
    assert to_delete == []
    assert plan["unchanged"] == 1


def test_changed_rights_are_written():
    desired = [{"Pin": "1001", "AuthorizeTimezoneId": 1, "AuthorizeDoorId": 3}]
    panel = [{"Pin": "1001", "AuthorizeTimezoneId": "1", "AuthorizeDoorId": "1"}]

    to_write, _to_delete, plan = plan_authorize_writes(desired, panel)

    assert to_write == desired
    assert plan["updated"] == 1


def test_a_person_missing_from_the_panel_gets_their_rights():
    desired = [{"Pin": "2150141426", "AuthorizeTimezoneId": 1, "AuthorizeDoorId": 1}]

    to_write, _to_delete, plan = plan_authorize_writes(desired, [])

    assert to_write == desired
    assert plan["updated"] == 1


# -- taking access away ----------------------------------------------------
def test_narrowing_a_level_removes_the_door_that_went_away():
    """Rights that are no longer granted have to be deleted, not just left behind."""
    desired = [{"Pin": "1001", "AuthorizeTimezoneId": 1, "AuthorizeDoorId": 1}]
    panel = [{"Pin": "1001", "AuthorizeTimezoneId": "1", "AuthorizeDoorId": "3"}]  # doors 1 and 2

    to_write, to_delete, _plan = plan_authorize_writes(desired, panel, revoke_pins={"1001"})

    assert to_write == desired
    assert to_delete == panel


def test_a_panel_the_person_has_left_loses_their_rights_here():
    """The reported bug, exactly.

    Moving somebody from `TEKNISI/IT` to `AKSES UMUM KARYAWAN` leaves RUANGAN SERVER
    with no desired rows for them. The sweep still visits it, and the old grant has
    to be removed — otherwise the card keeps opening the room it lost access to.
    """
    panel = [{"Pin": "2150141426", "AuthorizeTimezoneId": "1", "AuthorizeDoorId": "1"}]

    to_write, to_delete, plan = plan_authorize_writes([], panel, revoke_pins={"2150141426"})

    assert to_write == []
    assert to_delete == panel
    assert plan["revoked"] == 1


def test_nothing_is_removed_unless_the_person_was_named():
    """A whole-panel push must stay additive.

    Panels hold people this database cannot account for — OK PETUGAS carries 405
    users against the 361 we can explain — so removing rights for pins we merely
    failed to derive would lock out strangers.
    """
    panel = [{"Pin": "999999", "AuthorizeTimezoneId": "1", "AuthorizeDoorId": "1"}]

    _to_write, to_delete, plan = plan_authorize_writes([], panel)

    assert to_delete == []
    assert plan["revoked"] == 0


def test_only_the_named_people_are_revoked():
    """Revocation follows the people the caller named, nobody else on the panel."""
    panel = [
        {"Pin": "1001", "AuthorizeTimezoneId": "1", "AuthorizeDoorId": "1"},
        {"Pin": "1002", "AuthorizeTimezoneId": "1", "AuthorizeDoorId": "1"},
    ]

    _to_write, to_delete, _plan = plan_authorize_writes([], panel, revoke_pins={"1001"})

    assert to_delete == [panel[0]]


def test_a_kept_person_on_a_shared_panel_is_untouched():
    """The person who still belongs keeps their rows while a colleague is revoked."""
    desired = [{"Pin": "1002", "AuthorizeTimezoneId": 1, "AuthorizeDoorId": 1}]
    panel = [
        {"Pin": "1001", "AuthorizeTimezoneId": "1", "AuthorizeDoorId": "1"},
        {"Pin": "1002", "AuthorizeTimezoneId": "1", "AuthorizeDoorId": "1"},
    ]

    to_write, to_delete, plan = plan_authorize_writes(desired, panel, revoke_pins={"1001", "1002"})

    assert to_write == []
    assert to_delete == [panel[0]]
    assert plan["unchanged"] == 1
    assert plan["revoked"] == 1


# The record encoding the agent finally puts on the wire is pinned next to the
# agent itself; see tests/test_zk_push_agent.py.
