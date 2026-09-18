"""End-to-end: add a person, put them in an access level.

This is the workflow an operator actually performs ("tambah user, masukin ke
group level-nya"), so it is tested as one flow rather than as two endpoints.
"""

from __future__ import annotations


def _group(client, name: str) -> int:
    response = client.post("/api/access-groups", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _person(client, employee_id: str = "999", **extra) -> dict:
    payload = {"employee_id": employee_id, "name": f"Orang {employee_id}", **extra}
    response = client.post("/api/personnel", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _names(client, person_id: int) -> list[str]:
    return client.get(f"/api/personnel/{person_id}").json()["access_group_names"]


def test_two_step_flow_create_then_assign(client):
    """Step 1: add personnel. Step 2: add them to an access level."""
    group_id = _group(client, "ICU")

    person = _person(client, "999")
    assert person["access_group_names"] == []

    added = client.post(
        f"/api/access-groups/{group_id}/members", json={"employee_id": "999"}
    ).json()
    assert added["added"] == 1

    members = client.get(f"/api/access-groups/{group_id}/members").json()
    assert [m["employee_id"] for m in members] == ["999"]
    assert _names(client, person["id"]) == ["ICU"]


def test_access_group_id_on_create_puts_person_in_the_group(client):
    """`access_group_id` must not be a silent no-op.

    The field is accepted by PersonnelCreate, so setting it has to actually
    create membership in `personnel_access_groups` — otherwise the operator sees
    a saved record that looks right but grants no access.
    """
    group_id = _group(client, "ICU")

    person = _person(client, "999", access_group_id=group_id)

    members = client.get(f"/api/access-groups/{group_id}/members").json()
    assert [m["employee_id"] for m in members] == ["999"], (
        "personel dibuat dengan access_group_id tapi tidak masuk keanggotaan grup"
    )
    assert _names(client, person["id"]) == ["ICU"]


def test_access_group_id_on_create_rejects_unknown_group(client):
    response = client.post(
        "/api/personnel", json={"employee_id": "999", "name": "X", "access_group_id": 999}
    )
    assert response.status_code == 404


def test_failed_create_leaves_no_orphan_person(client):
    """A rejected create must not persist a half-built person.

    Validation used to run *after* the insert was committed, so a typo'd group id
    returned 404 while the person row quietly existed.
    """
    client.post("/api/personnel", json={"employee_id": "999", "name": "X", "access_group_id": 999})

    assert client.get("/api/personnel").json() == []


def test_failed_update_leaves_person_untouched(client):
    _group(client, "ICU")
    person = _person(client, "999")

    response = client.patch(f"/api/personnel/{person['id']}", json={"access_group_ids": [999]})

    assert response.status_code == 404
    after = client.get(f"/api/personnel/{person['id']}").json()
    assert after["name"] == "Orang 999"
    assert after["access_group_names"] == []


def test_access_group_id_on_update_is_additive(client):
    """Adding one group must not silently drop the others already held."""
    icu = _group(client, "ICU")
    isolasi = _group(client, "ISOLASI")
    person = _person(client, "999")

    client.patch(f"/api/personnel/{person['id']}", json={"access_group_id": icu})
    assert _names(client, person["id"]) == ["ICU"]

    client.patch(f"/api/personnel/{person['id']}", json={"access_group_id": isolasi})
    assert _names(client, person["id"]) == ["ICU", "ISOLASI"]


def test_access_group_ids_replaces_the_set(client):
    """The plural field is how you 'move' somebody between levels."""
    icu = _group(client, "ICU")
    isolasi = _group(client, "ISOLASI")
    person = _person(client, "999", access_group_ids=[icu, isolasi])
    assert _names(client, person["id"]) == ["ICU", "ISOLASI"]

    client.patch(f"/api/personnel/{person['id']}", json={"access_group_ids": [isolasi]})
    assert _names(client, person["id"]) == ["ISOLASI"]
    assert client.get(f"/api/access-groups/{icu}/members").json() == []


def test_access_group_ids_empty_list_clears_access(client):
    icu = _group(client, "ICU")
    person = _person(client, "999", access_group_ids=[icu])

    client.patch(f"/api/personnel/{person['id']}", json={"access_group_ids": []})

    assert _names(client, person["id"]) == []
    assert client.get(f"/api/access-groups/{icu}/members").json() == []


def test_assigning_the_same_group_twice_is_idempotent(client):
    icu = _group(client, "ICU")
    person = _person(client, "999", access_group_ids=[icu])

    client.patch(f"/api/personnel/{person['id']}", json={"access_group_ids": [icu]})
    client.patch(f"/api/personnel/{person['id']}", json={"access_group_ids": [icu]})

    assert _names(client, person["id"]) == ["ICU"]
    assert client.get(f"/api/access-groups/{icu}").json()["member_count"] == 1


def test_new_person_appears_in_personnel_list_with_group(client):
    group_id = _group(client, "ICU")
    _person(client, "1000", access_group_id=group_id)

    listing = client.get("/api/personnel").json()
    by_badge = {p["employee_id"]: p for p in listing}
    assert by_badge["1000"]["access_group_names"] == ["ICU"]


def test_legacy_single_column_only_set_when_exactly_one_group(client):
    """`access_group_id` is a back-compat hint, not the source of truth."""
    icu = _group(client, "ICU")
    isolasi = _group(client, "ISOLASI")
    person = _person(client, "999", access_group_ids=[icu, isolasi])

    assert client.get(f"/api/personnel/{person['id']}").json()["access_group_id"] is None

    client.patch(f"/api/personnel/{person['id']}", json={"access_group_ids": [icu]})
    assert client.get(f"/api/personnel/{person['id']}").json()["access_group_id"] == icu
