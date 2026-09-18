"""The login screen and the role split, as wired in `app/static/dashboard.html`.

Wiring only: whether the server actually refuses an HR account is locked by
`tests/test_auth.py`, which drives the real endpoints. These tests exist because a
dashboard that merely *looks* protected is the failure mode that matters — an HR
account staring at a Device tab whose every request answers 403 reads as a broken
app, and an operator who never sees the "password default" warning keeps using it.
"""

from __future__ import annotations

import re

from app.models import ROLES


def _html(client) -> str:
    return client.get("/").text


def test_the_shell_itself_is_public_but_shows_nothing_until_you_log_in(anon_client):
    """`/` must be reachable without a session, or the login form could never load."""
    response = anon_client.get("/")

    assert response.status_code == 200
    html = response.text
    assert '<div id="app" hidden>' in html, "dashboard harus tersembunyi sebelum login"
    assert 'id="login-overlay" hidden' in html
    assert 'id="login-user"' in html
    assert 'id="login-pass"' in html and 'type="password"' in html
    assert 'id="btn-login"' in html
    # The login form is the only thing on screen, so it must have a way in and out.
    assert "await fetchMe()" in html
    assert "function startSession(me)" in html


def test_an_expired_session_sends_the_operator_back_to_the_login_form(client):
    html = _html(client)

    assert "if (res.status === 401)" in html, "api() harus menangani sesi yang habis"
    assert "showLogin('Sesi Anda sudah berakhir" in html


def test_the_tabs_each_role_sees_match_the_server(anon_client):
    """`ROLE_TABS` is the UI half of the split in `app/api/deps.py`."""
    html = _html(anon_client)

    assert "const ROLE_TABS = {" in html
    admin = re.search(r"admin: \[([^\]]*)\]", html)
    hr = re.search(r"hr: \[([^\]]*)\]", html)
    assert admin and hr, "ROLE_TABS tidak terbaca"

    admin_tabs = {tab.strip().strip("'\"") for tab in admin.group(1).split(",")}
    hr_tabs = {tab.strip().strip("'\"") for tab in hr.group(1).split(",")}

    # Every tab the dashboard knows about, and the ones an HR account must not get:
    # each of them is admin-only on the server (device CRUD, scan, user accounts).
    assert admin_tabs == {
        "devices",
        "levels",
        "personnel",
        "departments",
        "scan",
        "monitor",
        "users",
    }
    assert hr_tabs == {"levels", "personnel", "departments", "monitor"}
    assert not hr_tabs & {"devices", "scan", "users"}
    assert sorted(ROLES) == ["admin", "hr"], "role baru = ROLE_TABS wajib ditinjau"


def test_every_tab_button_has_a_panel_and_the_user_tab_starts_hidden(client):
    html = _html(client)

    tabs = re.findall(r'class="tab[^"]*" data-tab="([a-z]+)"', html)
    assert tabs, "tidak ada tombol tab"
    for tab in tabs:
        assert f'id="panel-{tab}"' in html, f"tab {tab} tidak punya panel"

    # Admin-only: unhidden by applyRole() for an admin, never present for HR.
    assert re.search(r'data-tab="users"\s+hidden', html)
    assert "tab.hidden = !allowed.includes(tab.dataset.tab)" in html


def test_the_header_shows_who_is_logged_in_and_offers_a_way_out(client):
    html = _html(client)

    assert 'id="who"' in html
    assert "$('#who').textContent" in html
    assert 'id="btn-logout"' in html and "/api/auth/logout" in html
    assert 'id="btn-change-pass"' in html and "'/api/auth/password'" in html
    assert "Perangkat lain yang masih login" in html, "harus jelas sesi lain diputus"


def test_the_default_password_warning_is_shown_until_it_is_changed(client):
    html = _html(client)

    assert 'id="default-pass-warning" hidden' in html
    assert "warning.hidden = !me.must_change_password" in html
    assert "password default" in html


def test_the_user_tab_can_create_roles_and_reset_passwords(client):
    html = _html(client)

    for element in ("u-username", "u-fullname", "u-password", "u-role", "u-body", "u-msg"):
        assert f'id="{element}"' in html, f"#{element} hilang"

    assert 'id="btn-u-add"' in html and "'/api/users'" in html
    assert "data-role-next" in html
    assert "data-active-next" in html
    assert "data-pass" in html and "{ password: values.password }" in html
    assert "data-del" in html
    # Empty state must match the six columns of the table.
    assert "<tr><th>Pengguna</th>" in html
    assert 'colspan="${USER_COLUMNS}"' in html and "const USER_COLUMNS = 6;" in html


def test_the_risky_user_actions_ask_first_in_the_page_not_with_a_browser_dialog(client):
    html = _html(client)
    for action in ("setUserRole", "setUserActive", "deleteUser"):
        block = re.search(rf"async function {action}\(button\) \{{(.*?)\n\}}", html, re.S)
        assert block, f"{action} tidak ditemukan"
        assert "askConfirm({" in block.group(1), f"{action} harus konfirmasi dulu"

    # Deleting an account is not "removing the person": say so, or the operator
    # will avoid the button out of fear of losing personnel data.
    assert "Data personel, departemen, dan access level" in html
    assert "TIDAK terpengaruh" in html


def test_a_hidden_overlay_is_really_hidden(client):
    """`hidden` alone loses to `.modal-overlay { display: flex }`.

    The browser's own `[hidden]` rule is weaker than a class that sets `display`,
    so without this rule the login box stays on screen **on top of** the dashboard
    after a successful login — the page looks logged out while it is logged in.
    """
    html = _html(client)

    assert ".modal-overlay[hidden] { display: none; }" in html


def test_the_dashboard_never_uses_the_browser_dialogs(client):
    """`confirm()`/`prompt()`/`alert()` silently return "cancelled" once a browser
    stops showing them, which looks exactly like a broken feature. Only the modals
    drawn in the page may be used — see `askConfirm`/`askForm`."""
    source = "\n".join(
        line
        for line in _html(client).splitlines()
        if not line.strip().startswith(("//", "*", "/*"))
    )

    for native in ("confirm(", "prompt(", "alert("):
        assert not re.search(rf"(?<![A-Za-z]){re.escape(native)}", source), f"ada {native}"
    assert "function askConfirm(" in source
    assert "function askForm(" in source
