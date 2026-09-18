"""HTTP-level tests for the department master."""

from __future__ import annotations


def _dept(client, name: str, **extra) -> int:
    response = client.post("/api/departments", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_create_and_list_departments(client):
    _dept(client, "Bidang Keperawatan", legacy_id=8, code="KEP")

    listed = client.get("/api/departments").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Bidang Keperawatan"
    assert listed[0]["legacy_id"] == 8
    assert listed[0]["personnel_count"] == 0


def test_duplicate_department_returns_409(client):
    _dept(client, "ICU")
    assert client.post("/api/departments", json={"name": "ICU"}).status_code == 409


def test_dashboard_page_has_an_add_department_form(client):
    """The department tab must be able to create one, not only list them.

    The API has had POST /api/departments all along, but the page only listed rows —
    so adding a department meant calling the API by hand. The form reuses the same
    endpoint, and the parent dropdown is filled by loadDepartments() so a new
    department can immediately be chosen as another one's parent.
    """
    html = client.get("/").text

    assert 'id="dep-name"' in html
    assert 'id="dep-parent"' in html  # parent picker, "(tingkat atas)" default
    assert 'id="btn-dep-add"' in html
    assert "api('/api/departments', 'POST', payload)" in html
    # The parent dropdown is (re)filled by the same loader that fills the personnel
    # form's department picker, so both stay in step after every refresh.
    assert "parentSelect.innerHTML" in html
    # A duplicate name is the one error an operator will actually hit; the server's
    # message (which names the department) is shown, not swallowed.
    assert "say(msg, 'Gagal: ' + err.message, true)" in html


def test_missing_department_returns_404(client):
    assert client.get("/api/departments/999").status_code == 404


def test_department_detail_shows_path_and_children(client):
    root = _dept(client, "RSMP PATROL")
    child = _dept(client, "Bidang Keperawatan", parent_id=root)
    _dept(client, "ICU", parent_id=child)

    detail = client.get(f"/api/departments/{child}").json()

    assert detail["full_path"] == "RSMP PATROL / Bidang Keperawatan"
    assert detail["parent_name"] == "RSMP PATROL"
    assert [c["name"] for c in detail["children"]] == ["ICU"]


def test_department_tree(client):
    root = _dept(client, "RSMP PATROL")
    mid = _dept(client, "Bidang Keperawatan", parent_id=root)
    _dept(client, "ICU", parent_id=mid)

    tree = client.get("/api/departments/tree").json()

    assert len(tree) == 1
    assert tree[0]["name"] == "RSMP PATROL"
    assert tree[0]["children"][0]["name"] == "Bidang Keperawatan"
    assert tree[0]["children"][0]["children"][0]["name"] == "ICU"


def test_update_department_and_cycle_guard(client):
    a = _dept(client, "A")
    b = _dept(client, "B", parent_id=a)

    assert client.patch(f"/api/departments/{b}", json={"name": "B2"}).json()["name"] == "B2"
    assert client.patch(f"/api/departments/{a}", json={"parent_id": b}).status_code == 409
    assert client.patch(f"/api/departments/{a}", json={"parent_id": a}).status_code == 409


def test_delete_department(client):
    department_id = _dept(client, "Kosong")

    assert client.delete(f"/api/departments/{department_id}").status_code == 204
    assert client.get("/api/departments").json() == []


def test_delete_department_in_use_returns_409(client):
    department_id = _dept(client, "ICU")
    client.post(
        "/api/personnel",
        json={"employee_id": "1", "name": "A", "department_id": department_id},
    )

    response = client.delete(f"/api/departments/{department_id}")

    assert response.status_code == 409
    assert "masih dipakai" in response.json()["detail"]


def test_personnel_records_department_name_and_id(client):
    department_id = _dept(client, "Bidang Keperawatan")

    person = client.post(
        "/api/personnel",
        json={"employee_id": "1", "name": "Imam", "department_id": department_id},
    ).json()

    assert person["department_id"] == department_id
    assert person["department"] == "Bidang Keperawatan"


def test_personnel_department_by_name_creates_it(client):
    person = client.post(
        "/api/personnel",
        json={"employee_id": "1", "name": "Imam", "department": "TEKNISI"},
    ).json()

    assert person["department"] == "TEKNISI"
    departments = client.get("/api/departments").json()
    assert [d["name"] for d in departments] == ["TEKNISI"]
    assert departments[0]["personnel_count"] == 1


def test_personnel_unknown_department_id_returns_404(client):
    response = client.post(
        "/api/personnel",
        json={"employee_id": "1", "name": "X", "department_id": 999},
    )

    assert response.status_code == 404


def test_rename_department_reflected_on_personnel(client):
    department_id = _dept(client, "Keperawatan")
    client.post(
        "/api/personnel",
        json={"employee_id": "1", "name": "Imam", "department_id": department_id},
    )

    client.patch(f"/api/departments/{department_id}", json={"name": "Bidang Keperawatan"})

    assert client.get("/api/personnel").json()[0]["department"] == "Bidang Keperawatan"


def test_clear_department_via_personnel_update(client):
    department_id = _dept(client, "ICU")
    person_id = client.post(
        "/api/personnel",
        json={"employee_id": "1", "name": "A", "department_id": department_id},
    ).json()["id"]

    cleared = client.patch(f"/api/personnel/{person_id}", json={"clear_department": True}).json()

    assert cleared["department_id"] is None
    assert cleared["department"] is None
