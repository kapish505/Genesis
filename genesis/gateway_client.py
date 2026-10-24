"""Synchronous tool-gateway client used by the spec engine.

The engine is synchronous because it also runs inside hosted agents, so this
client is too. Genesis executes specs in a worker thread (``asyncio.to_thread``);
hosted agents call it from their synchronous handler.
"""

from __future__ import annotations

from typing import Any

import httpx

from genesis.errors import GatewayCallError


class SyncGateway:
    def __init__(
        self,
        base_url: str,
        agent_id: str,
        token: str,
        *,
        hosts: list[str] | tuple[str, ...] = (),
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.token = token
        self.hosts = list(hosts)
        self.timeout_s = timeout_s
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        payload = dict(args)
        if tool in {"fetch", "extract"} and self.hosts:
            payload.setdefault("allowed_hosts", self.hosts)
        try:
            response = self._http().post(
                f"{self.base_url}/{tool}",
                json={"agent_id": self.agent_id, "payload": payload},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as exc:
            raise GatewayCallError(
                f"gateway unreachable: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("detail", ""))
                retryable = retryable or bool(body.get("retryable"))
            except ValueError:
                detail = response.text[:200]
            raise GatewayCallError(
                f"tool {tool} failed (HTTP {response.status_code}): {detail}",
                retryable=retryable,
                status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayCallError("gateway returned invalid JSON", retryable=True) from exc
