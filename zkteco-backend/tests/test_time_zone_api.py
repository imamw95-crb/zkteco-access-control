"""HTTP-level tests for time zones and for bulk-adding access level members."""

from __future__ import annotations


def _slot(day: int, start: str, end: str, slot: int = 1) -> dict:
    return {"day_of_week": day, "slot": slot, "start_time": start, "end_time": end}


def test_create_and_list_time_zones(client):
    created = client.post(
        "/api/time-zones", json={"name": "24 Jam", "device_timezone_id": 1, "is_24_hour": True}
    )
    assert created.status_code == 201
    assert created.json()["name"] == "24 Jam"

    listed = client.get("/api/time-zones").json()
    assert [z["name"] for z in listed] == ["24 Jam"]


def test_duplicate_time_zone_returns_409(client):
    payload = {"name": "A", "device_timezone_id": 1}
    assert client.post("/api/time-zones", json=payload).status_code == 201
    assert client.post("/api/time-zones", json=payload).status_code == 409
    assert (
        client.post("/api/time-zones", json={"name": "B", "device_timezone_id": 1}).status_code
        == 409
    )


def test_missing_time_zone_returns_404(client):
    assert client.get("/api/time-zones/999").status_code == 404


def test_24_hour_preset_endpoint(client):
    first = client.post("/api/time-zones/presets/24-hours")
    assert first.status_code == 200
    body = first.json()

    assert body["name"] == "24 Jam"
    assert body["device_timezone_id"] == 1
    assert body["is_24_hour"] is True
    assert len(body["week"]) == 7
    assert all(day["segments"] == ["00:00-23:59"] for day in body["week"])

    # calling it again must not duplicate
    second = client.post("/api/time-zones/presets/24-hours")
    assert second.json()["id"] == body["id"]


def test_week_view_only_shows_active_segments(client):
    created = client.post(
        "/api/time-zones",
        json={
            "name": "Jam Kerja",
            "device_timezone_id": 2,
            "slots": [_slot(1, "07:00", "12:00"), _slot(1, "13:00", "17:00", slot=2)],
        },
    ).json()

    # `id` is our own key; `device_timezone_id` is the panel slot number.
    assert created["id"] != created["device_timezone_id"]

    monday = client.get(f"/api/time-zones/{created['id']}").json()["week"][1]

    assert monday["day_name"] == "Mon"
    assert monday["segments"] == ["07:00-12:00", "13:00-17:00"]
    assert client.get(f"/api/time-zones/{created['id']}").json()["week"][0]["segments"] == []


def test_update_time_zone(client):
    zone_id = client.post("/api/time-zones", json={"name": "A", "device_timezone_id": 3}).json()[
        "id"
    ]

    updated = client.patch(
        f"/api/time-zones/{zone_id}",
        json={"name": "B", "slots": [_slot(0, "08:00", "16:00")]},
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == "B"
    assert updated.json()["week"][0]["segments"] == ["08:00-16:00"]


def test_delete_time_zone(client):
    zone_id = client.post("/api/time-zones", json={"name": "A", "device_timezone_id": 3}).json()[
        "id"
    ]

    assert client.delete(f"/api/time-zones/{zone_id}").status_code == 204
    assert client.get("/api/time-zones").json() == []


def test_access_level_exposes_its_time_zone(client):
    """Every migrated level points at slot 1, so the name should show up."""
    client.post("/api/time-zones/presets/24-hours")
    group_id = client.post("/api/access-groups", json={"name": "ICU"}).json()["id"]

    listed = client.get("/api/access-groups").json()
    detail = client.get(f"/api/access-groups/{group_id}").json()

    assert listed[0]["time_zone_name"] == "24 Jam"
    assert listed[0]["is_24_hour"] is True
    assert detail["time_zone_name"] == "24 Jam"


def test_set_and_read_group_time_zone(client):
    zone_id = client.post(
        "/api/time-zones", json={"name": "Jam Kerja", "device_timezone_id": 5}
    ).json()["id"]
    group_id = client.post("/api/access-groups", json={"name": "ICU"}).json()["id"]

    response = client.put(
        f"/api/access-groups/{group_id}/time-zone", json={"time_zone_id": zone_id}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Jam Kerja"

    fetched = client.get(f"/api/access-groups/{group_id}/time-zone").json()
    assert fetched["device_timezone_id"] == 5
    assert client.get(f"/api/access-groups/{group_id}").json()["time_zone_name"] == "Jam Kerja"


def test_group_time_zone_404_when_undefined(client):
    group_id = client.post("/api/access-groups", json={"name": "ICU"}).json()["id"]

    response = client.get(f"/api/access-groups/{group_id}/time-zone")

    assert response.status_code == 404
    assert "belum didefinisikan" in response.json()["detail"]


def test_set_group_time_zone_missing_group(client):
    zone_id = client.post("/api/time-zones", json={"name": "A", "device_timezone_id": 1}).json()[
        "id"
    ]

    assert (
        client.put("/api/access-groups/999/time-zone", json={"time_zone_id": zone_id}).status_code
        == 404
    )


def test_delete_time_zone_in_use_returns_409(client):
    client.post("/api/time-zones/presets/24-hours")
    client.post("/api/access-groups", json={"name": "ICU"})  # defaults to slot 1

    response = client.delete("/api/time-zones/1")

    assert response.status_code == 409
    assert "masih dipakai" in response.json()["detail"]


# -- bulk adding members ---------------------------------------------------
def test_bulk_add_members(client):
    """The 'multi tambah' flow: many badges in a single request."""
    group_id = client.post("/api/access-groups", json={"name": "AKSES UMUM"}).json()["id"]
    badges = [f"7000{i}" for i in range(1, 6)]
    for badge in badges:
        client.post("/api/personnel", json={"employee_id": badge, "name": f"Orang {badge}"})

    result = client.post(
        f"/api/access-groups/{group_id}/members", json={"employee_ids": badges}
    ).json()

    assert result["added"] == 5
    assert result["skipped"] == 0
    assert client.get(f"/api/access-groups/{group_id}").json()["member_count"] == 5


def test_bulk_add_reports_unknown_badges(client):
    group_id = client.post("/api/access-groups", json={"name": "A"}).json()["id"]
    client.post("/api/personnel", json={"employee_id": "1", "name": "Ada"})

    result = client.post(
        f"/api/access-groups/{group_id}/members",
        json={"employee_ids": ["1", "tidak-ada", "juga-tidak-ada"]},
    ).json()

    assert result["added"] == 1
    assert result["skipped"] == 2
