"""Pytest fixtures.

The database is switched to SQLite *before* the app package is imported, so the
tests never need PostgreSQL or a physical panel.
"""

from __future__ import annotations

import os

os.environ["DATABASE_URL"] = "sqlite:///./test_zkteco.db"
os.environ["SCHEDULER_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import ROLE_ADMIN, ROLE_HR, Device, Personnel  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeDeviceClient,
    install_fake_client,
    uninstall_fake_client,
)

#: Credentials the HTTP fixtures log in with. They have nothing to do with the
#: seeded production accounts — the schema fixture wipes the `users` table after
#: every test, so these accounts are created on demand. The passwords satisfy the
#: minimum length policy; the *shorter* seeded defaults (`admin`, `hr`) come from
#: AUTH_SEED_USERS and are exempt because seeding is the operator's own setup.
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin"
HR_USER = "hr"
HR_PASSWORD = "hr-hr"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()


@pytest.fixture(autouse=True)
def _isolate_push_agent(monkeypatch):
    """Keep the ambient PUSH_AGENT_* environment out of the tests.

    A developer who runs the push agent locally has PUSH_AGENT_URL set, which would
    otherwise silently reconfigure the code under test — the push tests would then
    try to reach a real panel. Tests that want an agent configure one explicitly.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "push_agent_url", "")
    monkeypatch.setattr(settings, "push_agent_token", "")


@pytest.fixture(autouse=True)
def fake_clients():
    install_fake_client()
    yield FakeDeviceClient
    uninstall_fake_client()


@pytest.fixture
def db() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def make_account(db: Session):
    """Create a login account (the `users` table is wiped after every test)."""

    def _make(username: str = ADMIN_USER, password: str = ADMIN_PASSWORD, role: str = ROLE_ADMIN):
        from app.services.auth_service import AuthService

        service = AuthService(db)
        return service.get_by_username(username) or service.create_user(
            username=username, password=password, role=role
        )

    return _make


def _log_in(client: TestClient, username: str, password: str) -> TestClient:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def anon_client() -> TestClient:
    """No session at all — the shape every unauthenticated caller sees."""
    # No `with` block: the lifespan (which starts APScheduler) is skipped.
    return TestClient(fastapi_app)


@pytest.fixture
def client(make_account) -> TestClient:
    """An **admin** session.

    Every HTTP test in this suite drives the feature it is about, not the login
    form, so this fixture signs in as an admin. The guards themselves are locked
    by `tests/test_auth.py`, which uses `anon_client` and `hr_client`.
    """
    make_account(ADMIN_USER, ADMIN_PASSWORD, ROLE_ADMIN)
    return _log_in(TestClient(fastapi_app), ADMIN_USER, ADMIN_PASSWORD)


@pytest.fixture
def hr_client(make_account) -> TestClient:
    """An `hr` session: people data yes, panel settings no."""
    make_account(HR_USER, HR_PASSWORD, ROLE_HR)
    return _log_in(TestClient(fastapi_app), HR_USER, HR_PASSWORD)


@pytest.fixture
def make_device(db: Session):
    def _make(name: str = "IGD Kiri", ip: str = "10.100.1.14", **kwargs) -> Device:
        device = Device(name=name, ip=ip, port=kwargs.pop("port", 4370), **kwargs)
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    return _make


@pytest.fixture
def make_person(db: Session):
    def _make(employee_id: str = "1001", name: str = "Budi", **kwargs) -> Personnel:
        person = Personnel(employee_id=employee_id, name=name, **kwargs)
        db.add(person)
        db.commit()
        db.refresh(person)
        return person

    return _make
