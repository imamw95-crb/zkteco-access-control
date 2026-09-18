"""API routers.

Guarding is centralised here and **fail-closed**: every router below is registered
with a login guard, so an endpoint added later is protected by default instead of
by remembering to add a dependency. `auth` is the only deliberate exception — you
cannot log in through an endpoint that itself requires a login.

On top of "must be logged in", anything that changes how a **panel** behaves is
admin-only: door control, door schedules, device CRUD, the network scan, a panel's
own network settings, jam akses (time zones) and user accounts. An `hr` account
manages people data — personnel (including the push), departments and access
levels. Reads are shared, because HR still needs the device list and the feed.

`tests/test_auth.py` walks every registered route and fails if one is reachable
without a session, which is what keeps this list honest.
"""

from fastapi import APIRouter, Depends

from app.api import (
    access_groups,
    auth,
    control,
    dashboard,
    departments,
    devices,
    logs,
    monitor,
    personnel,
    scan,
    time_zones,
    users,
)
from app.api.deps import require_admin, require_login

api_router = APIRouter(prefix="/api")

# Public: login, logout, who-am-I and changing your own password.
api_router.include_router(auth.router)

# `require_admin` already depends on the logged-in user, so it also covers the login check.
logged_in = [Depends(require_login)]
admin_only = [Depends(require_admin)]

api_router.include_router(users.router)  # admin-only (guard lives inside the router)
api_router.include_router(devices.router, dependencies=logged_in)
api_router.include_router(personnel.router, dependencies=logged_in)
api_router.include_router(personnel.devices_router, dependencies=logged_in)
api_router.include_router(access_groups.router, dependencies=logged_in)
api_router.include_router(departments.router, dependencies=logged_in)
api_router.include_router(time_zones.router, dependencies=logged_in)
api_router.include_router(time_zones.groups_router, dependencies=logged_in)
api_router.include_router(logs.router, dependencies=logged_in)
api_router.include_router(monitor.router, dependencies=logged_in)
api_router.include_router(control.router, dependencies=admin_only)
api_router.include_router(control.schedules_router, dependencies=admin_only)
api_router.include_router(scan.router, dependencies=admin_only)
api_router.include_router(dashboard.router, dependencies=logged_in)

__all__ = ["api_router"]
