"""Client for the Windows push agent.

The backend runs on Linux with 64-bit Python and therefore cannot load the
official 32-bit `plcommpro.dll` that writing to a panel requires. Writing is
delegated to a small agent (`agent/zk_push_agent.py`) running on Windows, and
this module is the only place that talks to it.

Everything here is transport. Deciding *what* to write is
`app.services.panel_push`.
"""

from __future__ import annotations

import httpx

from app.config import settings


class PushAgentError(RuntimeError):
    """The agent rejected the request or could not be reached."""


class PushAgentNotConfigured(PushAgentError):
    """No agent URL configured, so pushing is impossible by definition."""


class PushAgentClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ):
        # `None` means "use the configured default"; an explicit empty string means
        # "no agent", which is what a caller that wants it disabled passes.
        url = settings.push_agent_url if base_url is None else base_url
        self.base_url = (url or "").rstrip("/")
        self.token = settings.push_agent_token if token is None else token
        self.timeout = settings.push_agent_timeout if timeout is None else timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self.configured:
            raise PushAgentNotConfigured(
                "PUSH_AGENT_URL belum diisi, jadi data tidak bisa dikirim ke panel. "
                "Jalankan agent/zk_push_agent.py di server Windows lalu set "
                "PUSH_AGENT_URL ke alamatnya."
            )

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise PushAgentError(f"Tidak bisa menghubungi push agent: {exc}") from exc

        payload: dict = {}
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = {"error": response.text}

        if response.status_code >= 400:
            raise PushAgentError(
                payload.get("error") or f"Push agent menjawab HTTP {response.status_code}"
            )
        return payload

    # -- operations --------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health")

    def read_table(
        self,
        ip: str,
        table: str,
        fields: list[str] | None = None,
        *,
        timeout_ms: int | None = None,
    ) -> list[dict]:
        """Read a device table.

        `timeout_ms` reaches the panel's connect timeout. It matters for big tables: a
        full `transaction` table takes long enough to stream that the 4 s default
        expires mid-transfer and the SDK answers `-2`, which looks like a busy panel.
        """
        params: dict = {}
        if fields:
            params["fields"] = ",".join(fields)
        if timeout_ms:
            params["timeout_ms"] = timeout_ms
        return self._request("GET", f"/panels/{ip}/tables/{table}", params=params or None).get(
            "records", []
        )

    def write_table(
        self,
        ip: str,
        table: str,
        records: list[dict],
        *,
        password: str | None = None,
    ) -> dict:
        body: dict = {"records": records}
        if password:
            body["password"] = password
        return self._request("POST", f"/panels/{ip}/tables/{table}", json=body)

    def delete_table(
        self,
        ip: str,
        table: str,
        records: list[dict],
        *,
        password: str | None = None,
    ) -> dict:
        body: dict = {"records": records, "delete": True}
        if password:
            body["password"] = password
        return self._request("POST", f"/panels/{ip}/tables/{table}", json=body)

    def control(self, ip: str, operation: int, **params: int) -> dict:
        return self._request(
            "POST", f"/panels/{ip}/control", json={"operation": operation, **params}
        )

    def set_params(self, ip: str, params: dict[str, str]) -> dict:
        """Write device parameters (``SetDeviceParam``) — used to re-address a panel.

        Only called by :mod:`app.services.panel_network`, which is switched off unless
        `PANEL_NETWORK_WRITE_ENABLED` is set: a wrong `IPAddress` leaves the panel
        unreachable, and unlike a bad personnel push it cannot be undone from here.
        """
        return self._request("POST", f"/panels/{ip}/params", json={"params": params})
