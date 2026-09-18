"""Service layer.

Routes must never talk to `zkaccess-c3` directly; they go through these
services, which in turn use a :class:`~app.services.device_client.DeviceClient`.
That keeps the network code mockable and the business logic testable.
"""

from app.services.c3_compat import apply_patches

# Apply the library workarounds once, at import time.
apply_patches()

__all__ = ["apply_patches"]
