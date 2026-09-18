"""End-to-end tests through the HTTP API (Swagger surface)."""

from __future__ import annotations

import json

from app.services import network_scan
from tests.fakes import fake_connect


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_openapi_schema_exposes_all_tags(client):
    schema = client.get("/openapi.json").json()
    tags = {
        tag
        for path in schema["paths"].values()
        for operation in path.values()
        for tag in operation.get("tags", [])
    }
    assert {"devices", "personnel", "logs", "control", "dashboard"} <= tags


def test_device_crud_over_http(client):
    created = client.post(
        "/api/devices", json={"name": "IGD Kiri", "ip": "10.100.1.14", "location": "IGD"}
    )
    assert created.status_code == 201
    device_id = created.json()["id"]

    assert len(client.get("/api/devices").json()) == 1

    patched = client.patch(f"/api/devices/{device_id}", json={"name": "IGD Kanan"})
    assert patched.json()["name"] == "IGD Kanan"

    assert client.delete(f"/api/devices/{device_id}").status_code == 204
    assert client.get("/api/devices").json() == []


def test_duplicate_device_returns_conflict(client):
    payload = {"name": "A", "ip": "10.100.1.14"}
    assert client.post("/api/devices", json=payload).status_code == 201
    assert client.post("/api/devices", json=payload).status_code == 409


def test_missing_device_returns_404(client):
    assert client.get("/api/devices/999").status_code == 404


def test_get_information_of_device(client, fake_clients):
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]
    fake_clients.users["10.100.1.14"] = [{"UID": str(i)} for i in range(1, 6)]

    response = client.post(f"/api/devices/{device_id}/info")

    assert response.status_code == 200
    body = response.json()
    assert body["serial_number"] == "TEST00000014"
    assert body["personnel_count"] == 5
    assert body["reachable"] is True


def test_device_info_returns_502_when_panel_down(client, fake_clients):
    device_id = client.post("/api/devices", json={"name": "Down", "ip": "10.100.1.99"}).json()["id"]
    fake_clients.online["10.100.1.99"] = False

    response = client.post(f"/api/devices/{device_id}/info")

    assert response.status_code == 502


def test_health_endpoint(client, fake_clients):
    device_id = client.post("/api/devices", json={"name": "Up", "ip": "10.100.1.10"}).json()["id"]

    body = client.post(f"/api/devices/{device_id}/health").json()

    assert body["is_online"] is True
    assert body["latency_ms"] == 12.5


def test_personnel_crud_over_http(client):
    created = client.post("/api/personnel", json={"employee_id": "1001", "name": "Budi"})
    assert created.status_code == 201
    person_id = created.json()["id"]

    duplicate = client.post("/api/personnel", json={"employee_id": "1001", "name": "X"})
    assert duplicate.status_code == 409

    renamed = client.patch(f"/api/personnel/{person_id}", json={"name": "Budi S"}).json()
    assert renamed["name"] == "Budi S"
    assert client.delete(f"/api/personnel/{person_id}").status_code == 204


def test_get_personnel_from_device(client, fake_clients):
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]
    fake_clients.users["10.100.1.14"] = [
        {"UID": "1001", "CardNo": "5001", "Pin": "1001", "Name": "Budi"}
    ]

    body = client.get(f"/api/devices/{device_id}/personnel").json()

    assert body["count"] == 1
    assert body["records"][0]["UID"] == "1001"


def test_personnel_count_endpoint(client, fake_clients):
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]
    fake_clients.users["10.100.1.14"] = [{"UID": str(i)} for i in range(1, 13)]

    body = client.get(f"/api/devices/{device_id}/personnel/count").json()

    assert body["personnel_count"] == 12


def test_push_personnel_without_agent_says_what_to_configure(client):
    """Writing needs the Windows agent; without it, explain rather than 501.

    501 used to be right — the pure-Python library genuinely had no write path.
    The capability exists now (the official SDK has it), it is simply not
    deployed yet, and 503 says exactly that.
    """
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]

    response = client.post(f"/api/devices/{device_id}/personnel/sync")

    assert response.status_code == 503
    assert "PUSH_AGENT_URL" in response.json()["detail"]


def test_push_personnel_dry_run_needs_no_agent(client):
    """`dry_run` must work with no agent at all — it never touches the network."""
    group = client.post("/api/access-groups", json={"name": "ICU"}).json()
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]
    client.post(
        f"/api/access-groups/{group['id']}/doors",
        json={"device_id": device_id, "door_number": 2},
    )
    client.post(
        "/api/personnel",
        json={
            "employee_id": "1001",
            "name": "Budi",
            "card_number": "5001",
            "access_group_ids": [group["id"]],
        },
    )

    body = client.post(f"/api/devices/{device_id}/personnel/sync?dry_run=true").json()

    assert body["dry_run"] is True
    assert body["device"]["users"] == 1
    assert body["sample_users"][0]["Pin"] == "1001"
    assert body["sample_users"][0]["CardNo"] == 5001
    # Door 2 is bit 1 of the panel's mask.
    assert body["sample_authorize"][0]["AuthorizeDoorId"] == 0b10


def test_sync_personnel_everywhere_reaches_every_covered_panel(client):
    """One person, one level, two panels — both must be written, and only those.

    Pushing per device left the other panels of the same access level stale, which
    is how a person ends up able to open one door out of the twenty they were
    granted.
    """
    level = client.post("/api/access-groups", json={"name": "TEKNISI/IT"}).json()
    server = client.post(
        "/api/devices", json={"name": "RUANGAN SERVER", "ip": "10.100.1.12"}
    ).json()
    igd = client.post("/api/devices", json={"name": "IGD Kiri", "ip": "10.100.1.14"}).json()
    client.post("/api/devices", json={"name": "GIZI", "ip": "10.100.1.9"})
    for device in (server, igd):
        client.post(
            f"/api/access-groups/{level['id']}/doors",
            json={"device_id": device["id"], "door_number": 1},
        )
    person = client.post(
        "/api/personnel",
        json={
            "employee_id": "2150141426",
            "name": "tes2150141426",
            "card_number": "2150141426",
            "access_group_ids": [level["id"]],
        },
    ).json()

    body = client.post(f"/api/personnel/sync?personnel_ids={person['id']}&dry_run=true").json()

    # Every panel is checked — a stale grant on one the person has left is exactly
    # what this has to find — but only the covered ones are meant to hold them.
    assert body["device_count"] == 3
    assert body["covered_count"] == 2
    covered = {row["device_name"] for row in body["results"] if row["covered"]}
    assert covered == {"RUANGAN SERVER", "IGD Kiri"}
    assert all(row["ok"] for row in body["results"])
    assert all(row["payload"]["users"] == 1 for row in body["results"] if row["covered"])
    assert all(row["payload"]["users"] == 0 for row in body["results"] if not row["covered"])


def test_sync_personnel_everywhere_without_agent_says_what_to_configure(client):
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]
    level = client.post("/api/access-groups", json={"name": "ICU"}).json()
    client.post(
        f"/api/access-groups/{level['id']}/doors",
        json={"device_id": device_id, "door_number": 1},
    )
    person = client.post(
        "/api/personnel",
        json={"employee_id": "1001", "name": "Budi", "access_group_ids": [level["id"]]},
    ).json()

    response = client.post(f"/api/personnel/sync?personnel_ids={person['id']}")

    assert response.status_code == 503
    assert "PUSH_AGENT_URL" in response.json()["detail"]


def test_sync_personnel_everywhere_rejects_an_empty_id_list(client):
    response = client.post("/api/personnel/sync?personnel_ids=,,")

    assert response.status_code == 422
    assert "kosong" in response.json()["detail"]


def test_sync_personnel_everywhere_rejects_junk_ids(client):
    response = client.post("/api/personnel/sync?personnel_ids=abc")

    assert response.status_code == 422


def _one_person_on_one_panel(client) -> dict:
    """A level, two panels and a person on one of them — enough for a sweep."""
    level = client.post("/api/access-groups", json={"name": "TEKNISI/IT"}).json()
    covered = client.post(
        "/api/devices", json={"name": "RUANGAN SERVER", "ip": "10.100.1.12"}
    ).json()
    client.post("/api/devices", json={"name": "GIZI", "ip": "10.100.1.9"})
    client.post(
        f"/api/access-groups/{level['id']}/doors",
        json={"device_id": covered["id"], "door_number": 1},
    )
    return client.post(
        "/api/personnel",
        json={
            "employee_id": "2150141426",
            "name": "tes2150141426",
            "access_group_ids": [level["id"]],
        },
    ).json()


def test_sync_personnel_streams_each_panel_then_a_summary(client):
    """The dashboard watches the sweep panel by panel.

    A sweep walks every panel in the estate, which is long enough that one final
    answer cannot tell progress from a hang — and cannot say which panels are
    already written. Each panel is therefore reported the moment it answers, with
    the summary still last so a sweep that went badly can say how far it got.
    """
    person = _one_person_on_one_panel(client)

    response = client.post(
        f"/api/personnel/sync?personnel_ids={person['id']}&dry_run=true&stream=true"
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.strip().splitlines()]
    assert [event["type"] for event in events] == ["panel", "panel", "summary"]

    panels = events[:-1]
    assert [panel["device_name"] for panel in panels] == ["GIZI", "RUANGAN SERVER"]
    assert [panel["covered"] for panel in panels] == [False, True]
    # `payload` is the whole derived record set for that panel (tens of kilobytes
    # each); the stream carries the counts and leaves the payload behind.
    assert all("payload" not in panel for panel in panels)

    summary = events[-1]
    assert summary["device_count"] == 2
    assert summary["covered_count"] == 1
    assert summary["failed"] == 0


def test_sync_personnel_stream_can_be_narrowed_to_named_panels(client):
    """Adding members to a level writes to that level's panels, not to the estate.

    The unscoped walk is what hunts down stale rights; a scoped one is what the
    operator asked for, and it has to report only the panels it visited.
    """
    person = _one_person_on_one_panel(client)
    devices = {device["name"]: device["id"] for device in client.get("/api/devices").json()}

    response = client.post(
        f"/api/personnel/sync?personnel_ids={person['id']}&dry_run=true&stream=true"
        f"&device_ids={devices['RUANGAN SERVER']}"
    )

    events = [json.loads(line) for line in response.text.strip().splitlines()]
    assert [event["type"] for event in events] == ["panel", "summary"]
    assert events[0]["device_name"] == "RUANGAN SERVER"
    assert events[-1]["device_count"] == 1
    assert events[-1]["covered_count"] == 1


def test_sync_personnel_rejects_junk_device_ids(client):
    response = client.post("/api/personnel/sync?personnel_ids=1&device_ids=abc")

    assert response.status_code == 422
    assert "device_ids" in response.json()["detail"]


def test_sync_personnel_stream_refuses_an_unconfigured_agent_before_it_starts(client):
    """A stream has already answered 200 by the time the first panel is written.

    Without this check a missing push agent would arrive as a stream of per-panel
    failures, which reads as "every panel is broken" instead of "the agent is not
    configured".
    """
    person = _one_person_on_one_panel(client)

    response = client.post(f"/api/personnel/sync?personnel_ids={person['id']}&stream=true")

    assert response.status_code == 503
    assert "PUSH_AGENT_URL" in response.json()["detail"]


def test_open_door_endpoint(client, fake_clients):
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]

    body = client.post(
        f"/api/control/devices/{device_id}/open", json={"door_number": 1, "duration_seconds": 7}
    ).json()

    assert body["success"] is True
    assert any(call.startswith("open_door:1:7") for call in fake_clients.created[0].history)


def test_logs_pull_and_search(client, fake_clients):
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]
    fake_clients.transactions["10.100.1.14"] = [
        {
            "Pin": "1001",
            "Verified": "1",
            "DoorID": "1",
            "EventType": "0",
            "InOutState": "2",
            "Time_second": 1789000000,
            "Index": "1",
        }
    ]

    pulled = client.post(f"/api/logs/devices/{device_id}/pull").json()
    assert pulled["inserted"] == 1

    listed = client.get("/api/logs").json()
    assert len(listed) == 1
    assert listed[0]["event_type"] == "NORMAL_PUNCH_OPEN"
    assert listed[0]["employee_id"] == "1001"

    again = client.post(f"/api/logs/devices/{device_id}/pull").json()
    assert again["duplicates"] == 1
    assert client.get("/api/logs/count").json()["count"] == 1


def test_realtime_endpoint(client, fake_clients):
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]

    events = client.get(f"/api/logs/devices/{device_id}/realtime").json()

    assert events[0]["event_type"] == "NORMAL_PUNCH_OPEN"


def test_dashboard_endpoints(client, fake_clients):
    created = client.post("/api/devices", json={"name": "IGD Kiri", "ip": "10.100.1.14"})
    device_id = created.json()["id"]
    fake_clients.users["10.100.1.14"] = [{"UID": "1"}, {"UID": "2"}, {"UID": "3"}]
    client.post(f"/api/devices/{device_id}/info")
    client.post("/api/personnel", json={"employee_id": "1001", "name": "Budi"})

    table = client.get("/api/dashboard/devices").json()
    assert table[0]["device_name"] == "IGD Kiri"
    assert table[0]["personnel_count"] == 3
    assert table[0]["status"] == "online"

    summary = client.get("/api/dashboard/summary").json()
    assert summary["devices_total"] == 1
    assert summary["devices_online"] == 1
    assert summary["personnel_total"] == 1
    assert summary["personnel_on_devices"] == 3


def test_discover_endpoint(client):
    found = client.get("/api/devices/discover").json()
    assert {item["ip"] for item in found} == {"10.100.1.90", "10.100.1.91"}


def test_door_schedule_crud(client):
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]

    created = client.post(
        "/api/door-schedules",
        json={
            "device_id": device_id,
            "door_number": 1,
            "day_of_week": 0,
            "start_time": "07:00",
            "end_time": "20:00",
        },
    )
    assert created.status_code == 201
    schedule_id = created.json()["id"]

    # same door + day updates instead of duplicating
    again = client.post(
        "/api/door-schedules",
        json={
            "device_id": device_id,
            "door_number": 1,
            "day_of_week": 0,
            "start_time": "08:00",
            "end_time": "21:00",
        },
    )
    assert again.json()["id"] == schedule_id
    assert again.json()["start_time"] == "08:00"

    assert len(client.get("/api/door-schedules").json()) == 1
    assert client.delete(f"/api/door-schedules/{schedule_id}").status_code == 204


def test_dashboard_page_pairs_a_save_with_the_panel_sync(client):
    """Saving personnel must also refresh the panels, and the wait must be visible.

    A panel keeps whichever rights it was last written with, so a dashboard edit
    that never reaches the hardware is silent: the operator sees the new access
    level while the door still opens for the old one. The page therefore carries
    a checkbox (on by default) that sends the person to every panel right after
    the save, sharing one code path with the manual "Samakan ke semua panel"
    button so both report the same outcome.
    """
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert html.count('id="p-sync-auto"') == 1
    assert "#p-sync-auto" in html  # read by the save handler and restored on reset
    assert "pushPersonEverywhere" in html

    # The sweep takes long enough that a spinner says nothing, so it is read as a
    # stream and listed panel by panel in a popup, with a "selesai" per panel.
    assert "stream=true" in html
    assert "function showPushProgress" in html
    assert "readPushStream" in html

    # A fleet-wide push walks 23 panels, so an immobile "mengirim..." reads as a
    # hang — and a second click queues another sweep against panels that accept
    # only one connection at a time.
    assert "sayBusy" in html
    assert 'class="spinner"' in html

    # The outcome is never a bare "done": a panel that failed still holds the old
    # data, and staying quiet about that is how somebody ends up at a locked door.
    assert "Panel BELUM diubah" in html
    assert "MASIH memakai data lama" in html


def test_dashboard_page_offers_the_panel_write_when_a_level_changes(client):
    """Adding a door or members to a level does not touch a single panel on its own.

    A level is dashboard data: the panel only learns about it when it is written to.
    So the Access Level card has to say what is still missing and offer that write —
    otherwise a level looks complete while the new door stays shut for exactly the
    people it was meant for.
    """
    html = client.get("/").text

    assert "async function pushPanel" in html
    assert "pushPanel(deviceId, fresh" in html  # a new door -> offer that panel
    assert "pushEverywhere(ids," in html  # new members -> offer the fleet sweep
    assert "ditambahkan, tapi panel" in html  # the card spells out what is missing
    assert "PANEL BELUM DIUBAH" in html
    # Members are written to THAT LEVEL's panels, not to the whole estate.
    assert "device_ids=" in html
    # Removing somebody has to offer the revoke, or the door keeps opening for them.
    assert "title: `Cabut hak ${personName} dari panel?`" in html


def test_dashboard_page_keeps_the_personnel_search_after_a_save(client):
    """Searching for the next person must keep working after an edit.

    Saving reloads the personnel table, which throws away every row's inline
    style. If the current filter is not applied again the box keeps showing the
    old query while all 531 rows are on screen — and because the box is not
    cleared either, the next search appends to the leftover text ("tes" + "budi")
    and matches nothing, which reads as "search is broken".
    """
    html = client.get("/").text

    assert "function applyPersonnelFilter" in html
    assert "applyPersonnelFilter();" in html  # re-applied by loadPersonnel()
    # Clicking the box selects the old query so typing replaces it.
    assert "addEventListener('focus', event => event.target.select())" in html


def test_dashboard_page_opens_on_the_personnel_tab(client):
    """The page opens on Personel, and the switch is the same one a click uses.

    Personnel is the daily job; the Device tab changes hardware. The highlighted
    button and the visible panel have to agree from the first paint, and the boot
    path has to go through selectTab() — a second copy of the switching logic is
    free to drift, and the drift shows up as an operator typing into a form they
    believe belongs to a different page.
    """
    html = client.get("/").text

    assert '<button class="tab active" data-tab="personnel">Personel</button>' in html
    assert '<button class="tab active" data-tab="devices">Device</button>' not in html
    assert '<section class="panel" id="panel-personnel">' in html  # visible
    assert '<section class="panel" id="panel-devices" hidden>' in html

    # One switch for both the click and the boot path.
    assert "function selectTab(name)" in html
    assert "selectTab('personnel');" in html
    # The device list is still loaded: it fills selects the other tabs use, and
    # nothing reloads it when the Device tab is opened.
    assert "loadDevices();" in html


def test_scan_endpoint_refuses_a_subnet_that_was_not_agreed_on(client):
    """A range sweep is visible on the network, so the allowed subnets are enforced."""
    response = client.post("/api/scan", json={"ranges": ["192.168.9.0/30"]})

    assert response.status_code == 422
    assert "diizinkan" in response.json()["detail"]


def test_scan_endpoint_lists_the_panels_that_answer(client, monkeypatch):
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))

    body = client.post("/api/scan", json={"ranges": ["10.100.1.4/30"], "identify": False}).json()

    assert body["hosts_scanned"] == 2
    assert body["hosts_open"] == 1
    assert body["hosts"][0]["ip"] == "10.100.1.5"


def test_scan_stream_is_ndjson(client, monkeypatch):
    """Two /24 ranges take seconds, so each batch is reported as it finishes."""
    monkeypatch.setattr(network_scan.socket, "create_connection", fake_connect({"10.100.1.5"}))

    response = client.post(
        "/api/scan?stream=true", json={"ranges": ["10.100.1.4/30"], "identify": False}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert [event["type"] for event in events] == ["progress", "host", "summary"]
    assert events[1]["ip"] == "10.100.1.5"
    assert events[-1]["hosts_open"] == 1


def test_scan_stream_validates_the_range_before_it_starts(client):
    """Once 200 is sent, a rejected range could only be reported inside the stream."""
    response = client.post("/api/scan?stream=true", json={"ranges": ["10.0.0.0/8"]})

    assert response.status_code == 422
    assert "terlalu banyak" in response.json()["detail"]


def test_scan_local_networks_endpoint(client):
    routes = client.get("/api/scan/local-networks").json()

    assert "10.100.1.0/24" in {item["range"] for item in routes}


def test_dashboard_page_has_a_separate_ip_range_scan(client):
    """Broadcast only reaches the backend's own subnet, so the range scan gets its own tab.

    Panels on a routed subnet (10.100.1.0/24 from a server sitting on 192.168.x) never
    answer a broadcast, so the operator needs somewhere to type the range, watch the
    sweep progress, and see which panels answered — without losing the quick broadcast
    button that still works on the Device tab.
    """
    html = client.get("/").text

    assert 'data-tab="scan"' in html
    assert 'id="panel-scan" hidden' in html  # its own tab, not mixed into Device
    assert 'id="sc-ranges"' in html
    assert "/api/scan?stream=true" in html
    assert "function readScanStream" in html
    # 250 addresses take long enough that a spinner alone reads as a hang.
    assert "host dipindai" in html
    # Registering a found device only touches this database, and the page says so —
    # elsewhere in the dashboard, saving does write to the panels.
    assert "PANEL BELUM DIUBAH" in html
    # No route to a subnet must be visible BEFORE the operator blames a panel.
    assert "TIDAK ADA RUTE" in html
    assert 'id="btn-dev-discover"' in html  # the broadcast search is still there


def test_dashboard_page_reports_a_scan_finding_by_serial_and_ip(client):
    """A panel whose address changed has to be recognisable, so serial is shown too."""
    html = client.get("/").text

    assert "hit.serial_number" in html
    assert "sudah terdaftar" in html
    assert 'id="sc-identify"' in html  # identification can be switched off (it is slow)
    assert "TIDAK ADA RUTE" in html


def test_dashboard_page_offers_the_record_fix_when_a_panel_moved(client):
    """A panel answering at another address means the database holds a stale IP.

    Every other feature dials that stored address, so the operator must be able to
    correct the record from the scan result. The button changes the DATABASE only —
    writing an address INTO a panel is deliberately not offered: a wrong value makes
    the panel unreachable and it can only be put right on site.
    """
    html = client.get("/").text

    assert "data-fixip" in html
    assert "Perbaiki IP di dashboard" in html
    assert "dashboard menyimpan" in html  # the drift is spelled out, not implied
    assert "PANEL TIDAK DIUBAH" in html
    # Adding a device from a scan carries the location/area with it.
    assert 'id="sc-location"' in html
    assert 'id="sc-area"' in html
    assert "askConfirm" in html  # never the browser's own confirm()


def test_dashboard_page_shows_the_panels_own_address_read_only(client):
    """The panel's own IP/netmask/gateway are shown so a drift can be spotted.

    Displaying them is a read (GETPARAM); writing them is a different, irreversible
    operation that is deliberately not offered from the dashboard.
    """
    html = client.get("/").text

    assert "IP di panel" in html
    assert "hit.panel_ip" in html
    assert "hit.panel_netmask" in html
    assert "hit.panel_gateway" in html
    assert "(beda)" in html  # the panel's own address vs the one it answered on


def test_a_panels_network_can_be_read_but_not_written_until_switched_on(client, monkeypatch):
    """ZKAccess' "Modify IP Address" is here, but switched off until an operator says so.

    Reading the panel's own configuration is always allowed (and is what makes a drift
    visible). Writing it is the one operation in this system that can leave a panel
    unreachable with no way back, so the default answer names the switch to flip and the
    safe alternative — and even a dry run reports `enabled: false`.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "panel_network_write_enabled", False)
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]
    body = {"ip": "10.100.1.30", "netmask": "255.255.255.0", "gateway": ""}

    read = client.get(f"/api/devices/{device_id}/network")
    assert read.status_code == 200
    assert read.json()["ip"] == "10.100.1.14"
    # The switch state travels with the read, so the page can say "NONAKTIF" up front.
    assert read.json()["write_enabled"] is False

    # dry_run defaults to TRUE, so a caller has to ask for the write explicitly.
    dry = client.post(f"/api/devices/{device_id}/network", json=body)
    assert dry.status_code == 200
    assert dry.json()["enabled"] is False
    assert dry.json()["written"] is False
    assert dry.json()["params"]["IPAddress"] == "10.100.1.30"

    write = client.post(f"/api/devices/{device_id}/network?dry_run=false", json=body)
    assert write.status_code == 503
    assert "PANEL_NETWORK_WRITE_ENABLED" in write.json()["detail"]


def test_an_address_outside_the_allowed_subnets_is_refused_over_http(client, monkeypatch):
    """A panel sent outside the reachable subnets could never be dialled again."""
    from app.config import settings

    monkeypatch.setattr(settings, "panel_network_write_enabled", True)
    device_id = client.post("/api/devices", json={"name": "IGD", "ip": "10.100.1.14"}).json()["id"]

    response = client.post(
        f"/api/devices/{device_id}/network",
        json={"ip": "192.168.9.30", "netmask": "255.255.255.0", "gateway": ""},
    )

    assert response.status_code == 422
    assert "SCAN_ALLOWED_NETWORKS" in response.json()["detail"]


def test_the_network_endpoints_404_on_an_unknown_device(client):
    assert client.get("/api/devices/999/network").status_code == 404
    assert (
        client.post(
            "/api/devices/999/network",
            json={"ip": "10.100.1.30", "netmask": "255.255.255.0", "gateway": ""},
        ).status_code
        == 404
    )


def test_dashboard_page_carries_the_panel_address_change(client):
    """The write button must be unreachable until the values have been dry-run.

    A wrong panel address cannot be undone from here, so the page makes the operator
    look at the values first: the button starts disabled, only a dry run of the exact
    same values enables it, and nothing is sent without a confirmation naming both
    addresses.
    """
    html = client.get("/").text

    assert 'id="nc-device"' in html
    assert 'id="btn-nc-write" disabled' in html
    assert 'id="nc-status"' in html  # saklar tampil sebelum tombol ditekan
    assert "PANEL_NETWORK_WRITE_ENABLED" in html
    assert "Cek dulu (dry run)" in html
    assert "networkPlanKey" in html  # nilai yang di-dry-run dikunci ke tombol tulis
    assert "dry_run=false" in html
    assert "tidak bisa dikembalikan dari sini" in html
    assert "askConfirm" in html
