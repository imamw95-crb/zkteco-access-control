"""Application configuration.

All settings can be overridden via environment variables or a `.env` file.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "ZKTeco C3 Access Control"

    # --- Database -------------------------------------------------------
    # Production (docker-compose) uses PostgreSQL.
    # Local dev / tests can override with e.g. sqlite:///./zkteco.db
    database_url: str = "postgresql+psycopg://zkteco:zkteco@db:5432/zkteco"

    # --- Device communication -------------------------------------------
    device_port: int = 4370
    device_connect_timeout: float = 4.0
    device_receive_timeout: float = 4.0
    device_receive_retries: int = 2
    # How many times a failed device call is retried before giving up.
    device_max_retries: int = 3
    # Base delay (seconds) for exponential backoff between retries.
    device_retry_backoff: float = 0.5
    # Max parallel device operations (sync / health check).
    device_parallelism: int = 8

    # --- Scheduler ------------------------------------------------------
    scheduler_enabled: bool = True
    # Access-log polling interval (seconds)
    log_poll_interval_seconds: int = 60
    # Device health-check interval (seconds)
    health_check_interval_seconds: int = 120

    # --- Push agent (writing to panels) ---------------------------------
    # Writing to a panel needs the official 32-bit `plcommpro.dll`, which this
    # 64-bit Linux backend cannot load. A separate Windows agent does the
    # writing; see agent/zk_push_agent.py. Leave the URL empty to keep pushing
    # disabled — every push then fails with a message explaining what to set up.
    push_agent_url: str = ""
    push_agent_token: str = ""
    push_agent_timeout: float = 60.0

    # --- Network scan (tab "Cari Device") -------------------------------
    #: Subnets that may be scanned, comma separated. A wide port sweep looks like
    #: an attack on a hospital network, so the limit is explicit — and it is an
    #: environment variable, so widening it never needs a code change.
    scan_allowed_networks: str = "10.100.1.0/24,192.168.1.0/24"
    #: Hard cap on addresses per scan: a typo like 10.0.0.0/8 would otherwise fire
    #: 16 million probes.
    scan_max_hosts: int = 4096
    #: Parallel TCP probes. Safe to parallelise — a bare connect says nothing to
    #: the panel's protocol, and identification is a separate, sequential step.
    scan_workers: int = 64

    # --- Writing a panel's own network configuration ---------------------
    #: OFF by default, and deliberately so. This is the equivalent of ZKAccess'
    #: "Modify IP Address" and the only operation in this system that can leave a
    #: panel unreachable **with no way back** — everything else can be retried or
    #: corrected from here, but a panel given a wrong address has to be fixed at the
    #: device itself. Turn it on only to move a panel you cannot reach by hand, and
    #: test on ONE panel first: writes still require a dry run of the exact values.
    panel_network_write_enabled: bool = False

    # --- Login / role-based access ---------------------------------------
    #: Name of the session cookie. The same value is accepted as
    #: `Authorization: Bearer <token>` so scripts and ops tools can log in too.
    auth_cookie_name: str = "zk_session"
    #: Session lifetime. Short enough that a shared operator PC does not stay
    #: logged in overnight; long enough for a full working shift.
    auth_session_hours: int = 12
    #: Turn on when the dashboard is served over HTTPS. Off by default because
    #: this deployment runs on plain HTTP inside the hospital LAN, and a `secure`
    #: cookie would then never be sent — the login would silently never stick.
    auth_cookie_secure: bool = False
    #: Accounts created **only when the `users` table is completely empty**,
    #: written as `username:password:role` separated by commas. Deliberately
    #: defaults to the passwords the user asked for; those accounts are flagged
    #: `must_change_password` so the dashboard keeps warning until they change it.
    auth_seed_users: str = "admin:admin:admin,hr:hr:hr"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
