"""Login, sessions and the role model.

Two roles, and the difference is *which part of the dashboard* a person may use —
never how much of the fleet they see:

* ``admin`` — everything: panels, door control, network scan, a panel's own network
  settings, jam akses (time zones), and user accounts.
* ``hr`` — the people side only: personnel (**including the push to panels**, which
  the operator explicitly asked for), departments, and access levels. Everything that
  only reads — monitoring, device list, logs — is open to both.

Enforcement is server-side in `app/api/deps.py`; hiding a tab in the dashboard is
convenience, never the lock. Anything that changes how a *panel* behaves (door
control, device CRUD, network scan, panel address, time zones, users) is admin-only.

Passwords use PBKDF2-HMAC-SHA256 from the standard library: no new dependency and
nothing to configure. Sessions are rows in ``auth_sessions`` instead of signed
cookies, so logout — or deactivating a user — really ends the session on the server.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import ROLE_ADMIN, ROLES, AuthSession, User

#: Cost of the password hash. 240k iterations is the OWASP figure for PBKDF2-SHA256
#: at the time of writing, and costs a few tens of milliseconds per login here.
PBKDF2_ITERATIONS = 240_000
_HASH_PREFIX = "pbkdf2_sha256"
#: Shortest accepted password. Not a policy fantasy — the seeded accounts have
#: 4-character passwords on purpose, so anything stricter would contradict them.
MIN_PASSWORD_LENGTH = 4
#: `last_seen_at` is written at most this often, otherwise every single API call
#: would do a write on the session row.
_LAST_SEEN_INTERVAL = timedelta(minutes=5)

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{2,64}$")


class AuthError(Exception):
    """Base class for every login/account failure."""


class InvalidCredentials(AuthError):
    """Wrong username or wrong password — deliberately the same message."""


class InactiveUser(AuthError):
    """The account exists but has been switched off."""


class DuplicateUsername(AuthError):
    pass


class UserNotFound(AuthError):
    pass


class LastAdmin(AuthError):
    """Refusing to leave the system with nobody who can administer it."""


class InvalidUsername(AuthError):
    pass


class WeakPassword(AuthError):
    pass


def _as_utc(value: datetime | None) -> datetime | None:
    """Re-label a naive timestamp read back from SQLite as UTC.

    SQLite cannot store a tz offset, so a value written with `timezone.utc` comes
    back naive, and comparing it with an aware `now()` raises `TypeError`. This
    codebase has already been bitten by exactly that on `Device.last_seen_at`.
    Everything in this module is written in UTC, so relabelling (not shifting) is
    the correct fix.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def hash_password(
    password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None
) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt>$<digest>`` (both base64)."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"{_HASH_PREFIX}${iterations}${salt_b64}${digest_b64}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. A malformed or missing hash simply never matches."""
    try:
        prefix, iterations, salt_b64, digest_b64 = (stored or "").split("$")
        if prefix != _HASH_PREFIX:
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iterations)
        )
    except (ValueError, TypeError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Hash to verify against when the username does not exist.

    Without it, a missing user answers noticeably faster than a wrong password,
    which turns the login form into a user-enumeration oracle.
    """
    return hash_password("tidak-ada-user-ini", iterations=PBKDF2_ITERATIONS)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalise_username(username: str) -> str:
    return (username or "").strip().lower()


class AuthService:
    def __init__(self, db: Session):
        self.db = db

    # -- login -------------------------------------------------------------
    def authenticate(self, username: str, password: str) -> User:
        """Check a username/password pair. Raises `AuthError` on any failure."""
        name = normalise_username(username)
        user = None
        if name:
            user = self.db.scalar(select(User).where(func.lower(User.username) == name))
        if user is None:
            verify_password(password, _dummy_hash())  # burn the same time as a real check
            raise InvalidCredentials("Nama pengguna atau password salah.")
        if not verify_password(password, user.password_hash):
            raise InvalidCredentials("Nama pengguna atau password salah.")
        if not user.is_active:
            raise InactiveUser(
                f"Akun '{user.username}' dinonaktifkan. Minta admin mengaktifkan kembali."
            )
        return user

    # -- sessions ----------------------------------------------------------
    def open_session(self, user: User, *, hours: int | None = None) -> str:
        """Create a session and return the RAW token (the only time it exists)."""
        from app.config import settings

        ttl = timedelta(hours=hours if hours is not None else settings.auth_session_hours)
        now = datetime.now(timezone.utc)
        self._purge_expired(user.id)
        token = secrets.token_urlsafe(32)
        self.db.add(
            AuthSession(
                token_hash=token_hash(token),
                user_id=user.id,
                expires_at=now + ttl,
                last_seen_at=now,
            )
        )
        user.last_login_at = now
        self.db.commit()
        return token

    def resolve(self, token: str) -> User | None:
        """The user behind a session token, or ``None`` if it is gone/expired."""
        if not token:
            return None
        session = self.db.scalar(
            select(AuthSession).where(AuthSession.token_hash == token_hash(token))
        )
        if session is None:
            return None
        now = datetime.now(timezone.utc)
        # The expiry is compared in Python: SQLite stores these as strings, and a
        # SQL comparison against a tz-aware bound parameter is not reliable there.
        expires_at = _as_utc(session.expires_at)
        if expires_at is None or expires_at <= now:
            self.db.delete(session)
            self.db.commit()
            return None
        user = self.db.get(User, session.user_id)
        if user is None or not user.is_active:
            self.db.delete(session)
            self.db.commit()
            return None
        last_seen = _as_utc(session.last_seen_at)
        if last_seen is None or now - last_seen > _LAST_SEEN_INTERVAL:
            session.last_seen_at = now
            self.db.commit()
        return user

    def close_session(self, token: str) -> None:
        if not token:
            return
        self.db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
        self.db.commit()

    def close_other_sessions(self, user_id: int, keep_token: str) -> int:
        """Kill every session of this user except the current one.

        Commits on purpose: without it the caller's session is closed by the
        request dependency and the DELETE is rolled back — the password change
        would look successful while every other device stayed logged in.
        """
        stmt = delete(AuthSession).where(
            AuthSession.user_id == user_id,
            AuthSession.token_hash != token_hash(keep_token or ""),
        )
        removed = self.db.execute(stmt).rowcount or 0
        self.db.commit()
        return removed

    def _purge_expired(self, user_id: int | None = None) -> int:
        """Delete expired session rows (for one user, or all of them).

        The comparison happens in Python on purpose — see `resolve()`.
        """
        now = datetime.now(timezone.utc)
        stmt = select(AuthSession)
        if user_id is not None:
            stmt = stmt.where(AuthSession.user_id == user_id)
        expired = [row for row in self.db.scalars(stmt) if (_as_utc(row.expires_at) or now) <= now]
        for row in expired:
            self.db.delete(row)
        if expired:
            self.db.commit()
        return len(expired)

    # -- accounts ----------------------------------------------------------
    def list_users(self) -> list[User]:
        return list(self.db.scalars(select(User).order_by(func.lower(User.username))))

    def get(self, user_id: int) -> User:
        user = self.db.get(User, user_id)
        if user is None:
            raise UserNotFound(f"Pengguna id {user_id} tidak ada.")
        return user

    def get_by_username(self, username: str) -> User | None:
        name = normalise_username(username)
        if not name:
            return None
        return self.db.scalar(select(User).where(func.lower(User.username) == name))

    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str = ROLE_ADMIN,
        full_name: str | None = None,
        must_change_password: bool = False,
        enforce_password_policy: bool = True,
    ) -> User:
        """Create an account.

        `enforce_password_policy=False` exists for one caller only: `seed_users()`,
        whose passwords come from the operator's own `AUTH_SEED_USERS`. That is why
        the shipped `hr` account can have a two-character password while a password
        typed into the Pengguna tab has to be at least four characters.
        """
        name = normalise_username(username)
        if not _USERNAME_RE.match(name):
            raise InvalidUsername(
                "Nama pengguna harus 2-64 karakter, hanya huruf, angka, titik, "
                "garis bawah atau strip."
            )
        if enforce_password_policy:
            self._assert_password(password)
        elif not password:
            raise WeakPassword("Password tidak boleh kosong.")
        self._assert_role(role)
        if self.get_by_username(name) is not None:
            raise DuplicateUsername(f"Nama pengguna '{name}' sudah dipakai.")
        user = User(
            username=name,
            password_hash=hash_password(password),
            role=role,
            full_name=(full_name or "").strip() or None,
            is_active=True,
            must_change_password=must_change_password,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def update_user(
        self,
        user_id: int,
        *,
        role: str | None = None,
        full_name: str | None = None,
        is_active: bool | None = None,
        password: str | None = None,
        must_change_password: bool | None = None,
    ) -> User:
        user = self.get(user_id)
        if role is not None:
            self._assert_role(role)
            if role != ROLE_ADMIN and user.role == ROLE_ADMIN:
                self._assert_not_last_admin(user, "mengubah role")
            user.role = role
        if is_active is not None:
            if not is_active and user.role == ROLE_ADMIN:
                self._assert_not_last_admin(user, "menonaktifkan")
            user.is_active = is_active
            if not is_active:
                # A switched-off account must not keep a live session; otherwise the
                # operator stays logged in until the cookie expires.
                self.db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
        if full_name is not None:
            user.full_name = full_name.strip() or None
        if password is not None:
            self._assert_password(password)
            user.password_hash = hash_password(password)
            user.must_change_password = bool(must_change_password)
            self.db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
        elif must_change_password is not None:
            user.must_change_password = must_change_password
        self.db.commit()
        self.db.refresh(user)
        return user

    def delete_user(self, user_id: int, *, actor_id: int | None = None) -> None:
        user = self.get(user_id)
        if actor_id is not None and user.id == actor_id:
            raise LastAdmin("Tidak bisa menghapus akun yang sedang Anda pakai sendiri.")
        if user.role == ROLE_ADMIN:
            self._assert_not_last_admin(user, "menghapus")
        self.db.delete(user)  # sessions cascade with it
        self.db.commit()

    def change_password(self, user: User, current_password: str, new_password: str) -> User:
        """A user changing their own password. Other sessions are revoked."""
        if not verify_password(current_password, user.password_hash):
            raise InvalidCredentials("Password lama salah.")
        self._assert_password(new_password)
        if verify_password(new_password, user.password_hash):
            raise WeakPassword("Password baru harus berbeda dari password lama.")
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        self.db.commit()
        self.db.refresh(user)
        return user

    # -- seeding -----------------------------------------------------------
    def seed_users(self, spec: str) -> list[User]:
        """Create the bootstrap accounts — **only** when there is no user at all.

        Never touches an existing account, so restarting the backend cannot reset a
        password that has been changed. Idempotent, like `_seed_presets()`.
        """
        if self.db.scalar(select(func.count()).select_from(User)):
            return []
        created: list[User] = []
        for chunk in (spec or "").split(","):
            parts = [p.strip() for p in chunk.split(":")]
            if len(parts) < 2 or not parts[0]:
                continue
            username, password = parts[0], parts[1]
            role = parts[2].lower() if len(parts) > 2 and parts[2] else ROLE_ADMIN
            if role not in ROLES:
                role = ROLE_ADMIN
            if self.get_by_username(username) is not None:
                continue
            created.append(
                self.create_user(
                    username=username,
                    password=password,
                    role=role,
                    full_name="Administrator" if role == ROLE_ADMIN else "HR / Kepegawaian",
                    must_change_password=True,
                    enforce_password_policy=False,
                )
            )
        return created

    # -- guards ------------------------------------------------------------
    @staticmethod
    def _assert_role(role: str) -> None:
        if role not in ROLES:
            raise InvalidUsername(f"Role harus salah satu dari: {', '.join(ROLES)}.")

    @staticmethod
    def _assert_password(password: str) -> None:
        if password is None or len(password) < MIN_PASSWORD_LENGTH:
            raise WeakPassword(f"Password minimal {MIN_PASSWORD_LENGTH} karakter.")

    def _assert_not_last_admin(self, user: User, action: str) -> None:
        others = self.db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == ROLE_ADMIN, User.is_active.is_(True), User.id != user.id)
        )
        if not others:
            raise LastAdmin(
                f"Menolak {action} '{user.username}': itu satu-satunya admin yang aktif. "
                "Buat admin lain dulu."
            )

    def count_admins(self) -> int:
        return int(
            self.db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role == ROLE_ADMIN, User.is_active.is_(True))
            )
            or 0
        )
