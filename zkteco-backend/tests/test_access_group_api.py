"""HTTP-level tests for access level (access group) endpoints."""

from __future__ import annotations


def _make_device(client, name: str = "ICU", ip: str = "10.100.1.11") -> int:
    return client.post("/api/devices", json={"name": name, "ip": ip}).json()["id"]


def _make_group(client, name: str = "ICU") -> int:
    response = client.post("/api/access-groups", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_access_level_crud_over_http(client):
    created = client.post("/api/access-groups", json={"name": "ICU", "description": "Ruang ICU"})
    assert created.status_code == 201
    group_id = created.json()["id"]
    assert created.json()["door_count"] == 0
    assert created.json()["member_count"] == 0

    assert client.post("/api/access-groups", json={"name": "ICU"}).status_code == 409

    listed = client.get("/api/access-groups").json()
    assert [g["name"] for g in listed] == ["ICU"]

    renamed = client.patch(f"/api/access-groups/{group_id}", json={"name": "ICU / NICU"})
    assert renamed.json()["name"] == "ICU / NICU"

    assert client.delete(f"/api/access-groups/{group_id}").status_code == 204
    assert client.get("/api/access-groups").json() == []


def test_access_level_missing_returns_404(client):
    assert client.get("/api/access-groups/999").status_code == 404


def test_access_level_doors_over_http(client):
    group_id = _make_group(client, "TEKNISI/IT")
    first = _make_device(client, "ICU", "10.100.1.11")
    second = _make_device(client, "IGD", "10.100.1.14")

    for device_id in (first, second):
        response = client.post(
            f"/api/access-groups/{group_id}/doors",
            json={"device_id": device_id, "door_number": 1},
        )
        assert response.status_code == 201

    doors = client.get(f"/api/access-groups/{group_id}/doors").json()
    assert len(doors) == 2
    assert doors[0]["device_name"]
    assert doors[0]["device_ip"]

    assert client.get(f"/api/access-groups/{group_id}").json()["door_count"] == 2

    assert client.delete(f"/api/access-groups/{group_id}/doors/{doors[0]['id']}").status_code == 204
    assert client.get(f"/api/access-groups/{group_id}").json()["door_count"] == 1


def test_access_level_add_door_unknown_device_returns_404(client):
    group_id = _make_group(client, "A")
    response = client.post(
        f"/api/access-groups/{group_id}/doors", json={"device_id": 999, "door_number": 1}
    )
    assert response.status_code == 404


def test_access_level_members_over_http(client):
    group_id = _make_group(client, "AKSES UMUM")
    for badge in ("1", "2", "3"):
        client.post("/api/personnel", json={"employee_id": badge, "name": f"Orang {badge}"})

    added = client.post(
        f"/api/access-groups/{group_id}/members",
        json={"employee_ids": ["1", "2", "3", "tidak-ada"]},
    ).json()
    assert added["added"] == 3
    assert added["skipped"] == 1

    members = client.get(f"/api/access-groups/{group_id}/members").json()
    assert [m["employee_id"] for m in members] == ["1", "2", "3"]
    assert members[0]["name"] == "Orang 1"

    person_id = members[0]["personnel_id"]
    assert client.delete(f"/api/access-groups/{group_id}/members/{person_id}").status_code == 204
    assert len(client.get(f"/api/access-groups/{group_id}/members").json()) == 2


def test_access_level_add_members_requires_a_target(client):
    group_id = _make_group(client, "A")
    assert client.post(f"/api/access-groups/{group_id}/members", json={}).status_code == 422


def test_access_level_members_pagination(client):
    group_id = _make_group(client, "A")
    for badge in ("1", "2", "3", "4", "5"):
        client.post("/api/personnel", json={"employee_id": badge, "name": f"Orang {badge}"})
    client.post(
        f"/api/access-groups/{group_id}/members", json={"employee_ids": ["1", "2", "3", "4", "5"]}
    )

    page = client.get(f"/api/access-groups/{group_id}/members?limit=2&offset=1").json()
    assert [m["employee_id"] for m in page] == ["2", "3"]


def test_access_level_detail_lists_doors_and_members(client):
    group_id = _make_group(client, "ICU")
    device_id = _make_device(client)
    client.post(f"/api/access-groups/{group_id}/doors", json={"device_id": device_id})
    client.post("/api/personnel", json={"employee_id": "202307056", "name": "Imam"})
    client.post(f"/api/access-groups/{group_id}/members", json={"employee_id": "202307056"})

    detail = client.get(f"/api/access-groups/{group_id}").json()

    assert detail["door_count"] == 1
    assert detail["member_count"] == 1
    assert detail["doors"][0]["device_name"] == "ICU"
    assert detail["members"][0]["employee_id"] == "202307056"
    assert detail["members"][0]["name"] == "Imam"


def test_person_appears_in_every_group_they_hold(client):
    client.post("/api/personnel", json={"employee_id": "1", "name": "Imam"})
    first = _make_group(client, "ICU")
    second = _make_group(client, "ISOLASI")
    for group_id in (first, second):
        client.post(f"/api/access-groups/{group_id}/members", json={"employee_id": "1"})

    person = client.get("/api/personnel").json()[0]
    assert person["access_group_names"] == ["ICU", "ISOLASI"]
