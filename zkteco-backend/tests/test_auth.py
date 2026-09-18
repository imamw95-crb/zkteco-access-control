"""Login, sessions and the admin/hr split.

These tests are about *access*, not about talking to hardware: no panel is dialled
(the autouse `fake_clients` fixture in `tests/conftest.py` installs the fake device
client), and nothing here touches `live_check.db`.

The two things that must stay true:

* the server refuses an unauthenticated request, and refuses a *non-admin* request
  for anything that changes how a panel behaves — hiding a tab in the dashboard is
  convenience, never the lock;
* an `hr` account can do the whole people workflow, including the push, because
  that is what the operator asked for.
"""

from __future__ import annotations

import re

from fastapi.routing import APIRoute
from sqlalchemy import select

from app.config import settings
from app.main import app as fastapi_app
from app.models import ROLE_ADMIN, ROLE_HR, AuthSession
from app.services.auth_service import (
    AuthService,
    LastAdmin,
    hash_password,
    token_hash,
    verify_password,
)

# ---------------------------------------------------------------------------
# The routing contract
# ---------------------------------------------------------------------------
#: Routes that must work *without* a session — you cannot log in through an
#: endpoint that itself demands a login, and logging out has to work precisely
#: when the session is already broken.
PUBLIC_ROUTES = {("POST", "/api/auth/login"), ("POST", "/api/auth/logout")}

LOGIN_GUARDS = {"require_login", "require_admin", "current_user"}

#: Everything that changes how a **panel** behaves, or who may use the system.
#: Locked here so a future endpoint cannot quietly lose its guard.
ADMIN_ONLY = [
    ("POST", "/api/devices"),
    ("GET", "/api/devices/discover"),
    ("PATCH", "/api/devices/{device_id}"),
    ("DELETE", "/api/devices/{device_id}"),
    ("POST", "/api/devices/{device_id}/info"),
    ("POST", "/api/devices/{device_id}/health"),
    ("POST", "/api/devices/sync/refresh"),
    ("POST", "/api/devices/{device_id}/network"),
    ("POST", "/api/control/devices/{device_id}/open"),
    ("POST", "/api/control/devices/{device_id}/cancel-alarm"),
    ("POST", "/api/control/devices/{device_id}/restart"),
    ("POST", "/api/control/devices/{device_id}/normal-open"),
    ("GET", "/api/control/devices/{device_id}/door-status"),
    ("GET", "/api/door-schedules"),
    ("POST", "/api/door-schedules"),
    ("DELETE", "/api/door-schedules/{schedule_id}"),
    ("GET", "/api/scan/local-networks"),
    ("POST", "/api/scan"),
    ("POST", "/api/time-zones"),
    ("POST", "/api/time-zones/presets/24-hours"),
    ("PATCH", "/api/time-zones/{time_zone_id}"),
    ("DELETE", "/api/time-zones/{time_zone_id}"),
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("PATCH", "/api/users/{user_id}"),
    ("DELETE", "/api/users/{user_id}"),
]


def _iter_api_routes(routes, prefix: str = ""):
    """Every `APIRoute` under `/api`, as `(route, full_path)`.

    `app.routes` does not hold the endpoints directly: FastAPI keeps an included
    router as a wrapper (`_IncludedRouter`) whose `include_context` carries the
    prefix to apply, and the endpoints inside carry only their own router's prefix.
    A flat scan therefore finds **nothing** — and a `startswith("/api")` filter
    applied to those inner paths throws everything away — which is why
    `test_the_route_walker_sees_the_whole_api` exists: without it, the two tests
    below would "pass" while inspecting an empty list.
    """
    for route in routes:
        context = getattr(route, "include_context", None)
        nested = getattr(route, "routes", None)
        if context is not None:
            yield from _iter_api_routes(
                getattr(route.original_router, "routes", []), prefix + (context.prefix or "")
            )
        elif nested:
            yield from _iter_api_routes(nested, prefix)
        elif isinstance(route, APIRoute):
            path = prefix + route.path
            if path.startswith("/api"):
                yield route, path


def _api_routes():
    for route, path in _iter_api_routes(fastapi_app.routes):
        for method in route.methods - {"HEAD", "OPTIONS"}:
            yield method, path, route


def _fill(path: str) -> str:
    """Replace `{device_id}` and friends with a harmless id — the guard must
    answer before anything looks the id up."""
    return re.sub(r"\{[^}]+\}", "1", path)


def _body(method: str) -> dict | None:
    return {} if method in {"POST", "PATCH", "PUT"} else None


def test_an_unauthenticated_caller_is_refused_everywhere(anon_client):
    """Walk the whole API and demand a 401 from every route but the login ones.

    This checks **behaviour, not wiring**: FastAPI applies router-level
    dependencies at match time, so a router registered with `dependencies=[...]`
    at include time is invisible in `route.dependant` — an inspection of the
    dependency graph would pass while the endpoints were wide open. Sending the
    request is the only honest check.
    """
    allowed = []
    for method, path, _ in _api_routes():
        if (method, path) in PUBLIC_ROUTES:
            continue
        response = anon_client.request(method, _fill(path), json=_body(method))
        if response.status_code != 401:
            allowed.append((method, path, response.status_code))

    assert allowed == [], f"Bisa diakses tanpa login: {allowed}"


def test_hr_is_refused_everything_that_touches_a_panel(hr_client):
    """The same sweep with an `hr` session: only admin-only routes, and 403."""
    wrong = []
    for method, path in ADMIN_ONLY:
        response = hr_client.request(method, _fill(path), json=_body(method))
        if response.status_code != 403:
            wrong.append((method, path, response.status_code))

    assert wrong == [], f"HR bisa membuka bagian admin: {wrong}"


def test_the_route_walker_sees_the_whole_api():
    """A guard for the guard: the sweeps above must not walk an empty list."""
    documented = {path for path in fastapi_app.openapi()["paths"] if path.startswith("/api")}
    found = {path for _, path, _ in _api_routes()}

    assert documented, "openapi.json tidak punya path /api sama sekali"
    assert found == documented


# ---------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------
def test_login_sets_a_cookie_and_answers_with_the_role(anon_client, make_account):
    make_account("admin", "rahasia-admin", ROLE_ADMIN)

    response = anon_client.post(
        "/api/auth/login", json={"username": "admin", "password": "rahasia-admin"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["role"] == ROLE_ADMIN
    assert response.json()["username"] == "admin"
    assert anon_client.cookies.get(settings.auth_cookie_name)
    # Neither the password nor its hash may come back — `must_change_password` is
    # just a flag, and it is the only field allowed to contain "password".
    assert "rahasia-admin" not in response.text
    assert "password_hash" not in response.text
    assert "pbkdf2" not in response.text


def test_login_is_case_insensitive_on_the_username(anon_client, make_account):
    make_account("Admin", "admin", ROLE_ADMIN)

    response = anon_client.post("/api/auth/login", json={"username": "ADMIN", "password": "admin"})

    assert response.status_code == 200, response.text
    assert response.json()["username"] == "admin"


def test_a_wrong_password_and_an_unknown_user_answer_the_same_way(anon_client, make_account):
    make_account("admin", "admin", ROLE_ADMIN)

    wrong = anon_client.post("/api/auth/login", json={"username": "admin", "password": "salah"})
    unknown = anon_client.post(
        "/api/auth/login", json={"username": "tidak-ada", "password": "salah"}
    )

    assert wrong.status_code == 401
    assert unknown.status_code == 401
    # Identical text on purpose: a different message would tell an attacker which
    # usernames exist.
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_a_deactivated_account_cannot_log_in(anon_client, make_account, db):
    user = make_account("rina", "rahasia-rina", ROLE_HR)
    AuthService(db).update_user(user.id, is_active=False)

    response = anon_client.post(
        "/api/auth/login", json={"username": "rina", "password": "rahasia-rina"}
    )

    assert response.status_code == 403
    assert "dinonaktifkan" in response.json()["detail"]


def test_me_needs_a_session(anon_client):
    response = anon_client.get("/api/auth/me")

    assert response.status_code == 401
    assert "login" in response.json()["detail"].lower()


def test_me_returns_the_logged_in_user(client):
    payload = client.get("/api/auth/me").json()

    assert payload["username"] == "admin"
    assert payload["role"] == ROLE_ADMIN


def test_a_bearer_token_works_for_scripts(make_account, db):
    """Ops scripts have no cookie jar, so the same session is accepted as a token."""
    from fastapi.testclient import TestClient

    make_account("admin", "admin", ROLE_ADMIN)
    service = AuthService(db)
    token = service.open_session(service.get_by_username("admin"))

    fresh = TestClient(fastapi_app)
    response = fresh.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    assert response.json()["username"] == "admin"


def test_logout_really_ends_the_session_on_the_server(client):
    """Not just "delete the cookie in the browser" — the row is gone."""
    from fastapi.testclient import TestClient

    stolen = client.cookies.get(settings.auth_cookie_name)
    assert client.post("/api/auth/logout").status_code == 204

    replay = TestClient(fastapi_app)
    replay.cookies.set(settings.auth_cookie_name, stolen)
    assert replay.get("/api/auth/me").status_code == 401


def test_an_expired_session_is_refused_and_cleaned_up(make_account, db):
    user = make_account("admin", "admin", ROLE_ADMIN)
    service = AuthService(db)
    expired = service.open_session(user, hours=-1)

    assert service.resolve(expired) is None
    # The dead row is deleted, so a long-lived deployment cannot accumulate them.
    left = db.scalars(
        select(AuthSession).where(AuthSession.token_hash == token_hash(expired))
    ).all()
    assert left == []


def test_the_database_never_holds_the_session_token(client, db):
    raw = client.cookies.get(settings.auth_cookie_name)
    db.expire_all()
    rows = db.scalars(select(AuthSession)).all()

    assert rows, "login seharusnya membuat satu baris auth_sessions"
    assert all(row.token_hash != raw for row in rows)
    assert any(row.token_hash == token_hash(raw) for row in rows)


# ---------------------------------------------------------------------------
# HR: the people side, and only that
# ---------------------------------------------------------------------------
def test_hr_can_manage_personnel_departments_and_levels(hr_client):
    person = hr_client.post("/api/personnel", json={"employee_id": "H-1", "name": "Rina HR"})
    assert person.status_code == 201, person.text

    assert hr_client.get("/api/personnel").status_code == 200
    assert hr_client.post("/api/departments", json={"name": "Kepegawaian"}).status_code == 201
    level = hr_client.post("/api/access-groups", json={"name": "HR UJI", "description": "pc"})
    assert level.status_code == 201, level.text


def test_hr_can_push_personnel_to_panels(hr_client, make_device, make_person, fake_clients):
    """Explicitly requested: HR does the personnel work, and saving pushes."""
    make_device()
    person = make_person("H-2", "Rina HR")

    response = hr_client.post(f"/api/personnel/sync?personnel_ids={person.id}&dry_run=true")

    assert response.status_code == 200, response.text


def test_hr_cannot_touch_panel_settings(hr_client, make_device):
    device = make_device()
    attempts = [
        ("post", "/api/devices", {"json": {"name": "X", "ip": "10.100.1.99"}}),
        ("patch", f"/api/devices/{device.id}", {"json": {"name": "X"}}),
        ("delete", f"/api/devices/{device.id}", {}),
        ("post", f"/api/devices/{device.id}/info", {}),
        ("post", f"/api/devices/{device.id}/network", {"json": {"new_ip": "10.100.1.99"}}),
        ("post", f"/api/control/devices/{device.id}/open", {"json": {"door_number": 1}}),
        ("get", "/api/door-schedules", {}),
        ("get", "/api/devices/discover", {}),
        ("post", "/api/scan", {"json": {"range": "10.100.1.0/30"}}),
        ("post", "/api/time-zones", {"json": {"name": "Jam HR"}}),
        ("patch", "/api/time-zones/1", {"json": {"name": "Jam HR"}}),
    ]
    for method, url, kwargs in attempts:
        response = getattr(hr_client, method)(url, **kwargs)
        assert response.status_code == 403, f"{method.upper()} {url} → {response.status_code}"


def test_hr_can_read_the_things_it_needs_to_pick_a_door(hr_client, make_device):
    make_device()

    assert hr_client.get("/api/devices").status_code == 200
    assert hr_client.get("/api/time-zones").status_code == 200
    assert hr_client.get("/api/monitor/snapshot").status_code == 200
    assert hr_client.get("/api/dashboard/summary").status_code == 200


def test_hr_cannot_open_the_user_accounts(hr_client):
    assert hr_client.get("/api/users").status_code == 403
    assert (
        hr_client.post(
            "/api/users", json={"username": "sindikat", "password": "rahasia", "role": ROLE_ADMIN}
        ).status_code
        == 403
    )


def test_admin_can_do_what_hr_cannot(client, make_device):
    device = make_device()

    assert client.get("/api/users").status_code == 200
    assert client.get(f"/api/devices/{device.id}").status_code == 200
    assert client.get("/api/door-schedules").status_code == 200
    created = client.post("/api/time-zones", json={"name": "Jam Admin", "device_timezone_id": 9})
    assert created.status_code == 201, created.text


# ---------------------------------------------------------------------------
# Managing accounts (admin only)
# ---------------------------------------------------------------------------
def test_an_admin_creates_and_lists_accounts(client):
    created = client.post(
        "/api/users", json={"username": "rina", "password": "rahasia-rina", "role": ROLE_HR}
    )

    assert created.status_code == 201, created.text
    assert created.json()["role"] == ROLE_HR
    assert "rahasia-rina" not in created.text
    usernames = [user["username"] for user in client.get("/api/users").json()]
    assert "rina" in usernames


def test_a_duplicate_username_is_refused(client):
    client.post("/api/users", json={"username": "rina", "password": "rahasia", "role": ROLE_HR})

    again = client.post(
        "/api/users", json={"username": "RINA", "password": "rahasia", "role": ROLE_HR}
    )

    assert again.status_code == 409
    assert "sudah dipakai" in again.json()["detail"]


def test_a_short_password_and_a_typo_role_are_refused(client):
    short = client.post("/api/users", json={"username": "rina", "password": "123", "role": ROLE_HR})
    role = client.post(
        "/api/users", json={"username": "rina", "password": "rahasia", "role": "bos"}
    )
    name = client.post(
        "/api/users", json={"username": "ab cd", "password": "rahasia", "role": ROLE_HR}
    )

    assert short.status_code == 400
    assert "minimal" in short.json()["detail"]
    assert role.status_code == 400
    assert ROLE_HR in role.json()["detail"]
    assert name.status_code == 400
    assert "Nama pengguna" in name.json()["detail"]


def test_deactivating_an_account_also_kills_its_sessions(client, make_account, db):
    from fastapi.testclient import TestClient

    make_account("rina", "rahasia", ROLE_HR)
    rina = TestClient(fastapi_app)
    assert (
        rina.post("/api/auth/login", json={"username": "rina", "password": "rahasia"}).status_code
        == 200
    )
    user = AuthService(db).get_by_username("rina")

    assert client.patch(f"/api/users/{user.id}", json={"is_active": False}).status_code == 200

    assert rina.get("/api/auth/me").status_code == 401
    assert (
        rina.post("/api/auth/login", json={"username": "rina", "password": "rahasia"}).status_code
        == 403
    )


def test_resetting_a_password_kills_the_old_sessions(client, make_account, db):
    from fastapi.testclient import TestClient

    make_account("rina", "rahasia", ROLE_HR)
    rina = TestClient(fastapi_app)
    rina.post("/api/auth/login", json={"username": "rina", "password": "rahasia"})
    user = AuthService(db).get_by_username("rina")

    assert (
        client.patch(f"/api/users/{user.id}", json={"password": "baru-sekali"}).status_code == 200
    )

    assert rina.get("/api/auth/me").status_code == 401
    assert (
        rina.post(
            "/api/auth/login", json={"username": "rina", "password": "baru-sekali"}
        ).status_code
        == 200
    )


def test_the_last_admin_cannot_be_demoted_or_switched_off(client, db):
    """Otherwise nobody could administer the system any more."""
    admin = AuthService(db).get_by_username("admin")

    demote = client.patch(f"/api/users/{admin.id}", json={"role": ROLE_HR})
    off = client.patch(f"/api/users/{admin.id}", json={"is_active": False})

    for response in (demote, off):
        assert response.status_code == 409, response.text
        assert "admin" in response.json()["detail"]


def test_you_cannot_delete_the_account_you_are_logged_in_with(client, db):
    """Also 409 — but for a different reason, so it gets its own message."""
    admin = AuthService(db).get_by_username("admin")
    client.post(
        "/api/users", json={"username": "admin2", "password": "rahasia", "role": ROLE_ADMIN}
    )

    response = client.delete(f"/api/users/{admin.id}")

    assert response.status_code == 409
    assert "sendiri" in response.json()["detail"]


def test_a_second_admin_unlocks_the_first(client, db):
    """The guard is about the *last* admin, not about the first one."""
    admin = AuthService(db).get_by_username("admin")
    client.post(
        "/api/users", json={"username": "admin2", "password": "rahasia", "role": ROLE_ADMIN}
    )

    assert client.patch(f"/api/users/{admin.id}", json={"role": ROLE_HR}).status_code == 200
    assert AuthService(db).count_admins() == 1


def test_an_admin_cannot_delete_the_account_they_are_using(client, db):
    """Deleting yourself while logged in with it would leave you stranded."""
    admin = AuthService(db).get_by_username("admin")
    client.post(
        "/api/users", json={"username": "admin2", "password": "rahasia", "role": ROLE_ADMIN}
    )

    response = client.delete(f"/api/users/{admin.id}")

    assert response.status_code == 409


def test_deleting_an_account_removes_it(client):
    created = client.post(
        "/api/users", json={"username": "rina", "password": "rahasia", "role": ROLE_HR}
    ).json()

    assert client.delete(f"/api/users/{created['id']}").status_code == 204
    assert client.delete(f"/api/users/{created['id']}").status_code == 404


def test_changing_a_missing_account_is_a_404(client):
    assert client.patch("/api/users/424242", json={"role": ROLE_HR}).status_code == 404


# ---------------------------------------------------------------------------
# Changing your own password
# ---------------------------------------------------------------------------
def test_changing_your_own_password_keeps_this_session_and_drops_the_others(
    client, make_account, db
):
    """A changed password has to log the *other* devices out; this one keeps going."""
    from fastapi.testclient import TestClient

    make_account("rina", "rahasia", ROLE_HR)
    phone = TestClient(fastapi_app)
    phone.post("/api/auth/login", json={"username": "rina", "password": "rahasia"})
    laptop = TestClient(fastapi_app)
    laptop.post("/api/auth/login", json={"username": "rina", "password": "rahasia"})

    response = laptop.post(
        "/api/auth/password", json={"current_password": "rahasia", "new_password": "lebih-panjang"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["must_change_password"] is False
    assert laptop.get("/api/auth/me").status_code == 200
    assert phone.get("/api/auth/me").status_code == 401


def test_changing_a_password_needs_the_old_one(client):
    response = client.post(
        "/api/auth/password", json={"current_password": "salah", "new_password": "apa-saja"}
    )

    assert response.status_code == 400
    assert "Password lama salah" in response.json()["detail"]


def test_the_same_password_again_is_refused(client):
    response = client.post(
        "/api/auth/password", json={"current_password": "admin", "new_password": "admin"}
    )

    assert response.status_code == 400
    assert "berbeda" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Hashing and the seeded accounts
# ---------------------------------------------------------------------------
def test_a_password_hash_verifies_but_is_never_the_password():
    stored = hash_password("rahasia")

    assert "rahasia" not in stored
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("rahasia", stored)
    assert not verify_password("Rahas1a", stored)


def test_two_equal_passwords_get_different_hashes():
    """Per-password salt: identical passwords must not look identical in the table."""
    assert hash_password("rahasia") != hash_password("rahasia")


def test_a_broken_hash_simply_never_matches():
    for broken in ["", None, "plaintext", "pbkdf2_sha256$bukan-angka$a$b", "md5$1$a$b"]:
        assert verify_password("rahasia", broken) is False


def test_seeding_creates_both_accounts_only_once(db):
    service = AuthService(db)

    created = service.seed_users("admin:admin:admin,hr:hr:hr")
    again = service.seed_users("admin:admin:admin,hr:hr:hr")

    assert sorted(user.username for user in created) == ["admin", "hr"]
    assert again == [], "seeding ulang tidak boleh mengubah apa pun"
    # A password that was changed must survive a restart, so seeding must not
    # touch existing accounts.
    service.change_password(service.get_by_username("admin"), "admin", "ganti-dong")
    assert AuthService(db).seed_users("admin:admin:admin,hr:hr:hr") == []
    assert verify_password("ganti-dong", AuthService(db).get_by_username("admin").password_hash)


def test_seeded_accounts_get_the_roles_from_the_spec_and_a_warning_flag(db):
    created = {
        user.username: user for user in AuthService(db).seed_users("admin:admin:admin,hr:hr:hr")
    }

    assert created["admin"].role == ROLE_ADMIN
    assert created["hr"].role == ROLE_HR
    assert all(user.must_change_password for user in created.values())


def test_the_seed_spec_skips_junk_and_defaults_the_role_to_admin(db):
    created = AuthService(db).seed_users(" ,putri:rahasia, rusak")

    assert [user.username for user in created] == ["putri"]
    assert created[0].role == ROLE_ADMIN


def test_an_unknown_role_in_the_spec_falls_back_to_admin(db):
    """A typo in AUTH_SEED_USERS must not create an account that cannot log in."""
    created = AuthService(db).seed_users("tuan:tuan:supervisor")

    assert created[0].role == ROLE_ADMIN


def test_last_admin_error_is_raised_by_the_service_too(db):
    """The HTTP layer maps it to 409, but the rule itself lives in the service."""
    service = AuthService(db)
    service.create_user(username="admin", password="admin", role=ROLE_ADMIN)
    admin = service.get_by_username("admin")

    for call in (
        lambda: service.update_user(admin.id, role=ROLE_HR),
        lambda: service.update_user(admin.id, is_active=False),
        lambda: service.delete_user(admin.id),
    ):
        try:
            call()
        except LastAdmin:
            continue
        raise AssertionError("aturan 'admin terakhir' tidak ditegakkan")


def test_a_password_change_clears_the_warning_flag(db):
    service = AuthService(db)
    user = service.create_user(
        username="rina", password="rahasia", role=ROLE_HR, must_change_password=True
    )

    service.change_password(user, "rahasia", "yang-baru")

    assert service.get(user.id).must_change_password is False
