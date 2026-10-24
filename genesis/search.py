"""Agentverse REST clients.

Shapes follow the published Agentverse API and the agentverse-skills scripts:

* ``POST /v1/search/agents`` with ``filters``, ``search_text``, ``sort``, ``limit``;
  results arrive under ``data["agents"]``;
* hosting: ``POST /v1/hosting/agents`` (create), ``PUT /v1/hosting/agents/{addr}/code``,
  ``POST .../start``, ``POST .../stop``, ``GET .../logs``, ``DELETE /v1/hosting/agents/{addr}``.

Every endpoint in this module is isolated here so a platform change touches one
file. All calls retry only on 429/5xx/timeouts.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)


class AgentverseError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, AgentverseError):
        return exc.retryable
    return False


class AgentverseClient:
    """Thin async wrapper. A caller may inject a transport for tests."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = client
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        if not self.configured:
            raise AgentverseError("Agentverse API key is not configured", retryable=False)

        async def _call() -> httpx.Response:
            response = await self._http().request(
                method, f"{self.base_url}{path}", headers=self.headers(), json=json
            )
            if response.status_code in {401, 403}:
                raise AgentverseError(
                    f"Agentverse rejected the API key (HTTP {response.status_code})",
                    status=response.status_code,
                    retryable=False,
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise AgentverseError(
                    f"Agentverse returned HTTP {response.status_code}",
                    status=response.status_code,
                    retryable=True,
                )
            if response.status_code >= 400:
                raise AgentverseError(
                    f"Agentverse returned HTTP {response.status_code}: {response.text[:300]}",
                    status=response.status_code,
                    retryable=False,
                )
            return response

        try:
            async for attempt in AsyncRetrying(
                reraise=True,
                stop=stop_after_attempt(3),
                wait=wait_exponential_jitter(initial=1, max=10),
                retry=retry_if_exception(_retryable),
            ):
                with attempt:
                    response = await _call()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise AgentverseError("Agentverse is unreachable", retryable=True) from exc
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise AgentverseError("Agentverse returned invalid JSON") from exc

    # ------------------------------------------------------------------ search

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        state: list[str] | None = None,
        has_readme: bool = True,
    ) -> list[dict[str, Any]]:
        body = {
            "filters": {
                "state": state or ["active"],
                "category": [],
                "agent_type": [],
                "protocol_digest": [],
                "has_readme": has_readme,
            },
            "sort": "relevancy",
            "direction": "asc",
            "search_text": query[:200],
            "offset": 0,
            "limit": max(1, min(limit, 20)),
        }
        data = await self._request("POST", "/v1/search/agents", json=body)
        agents = data.get("agents", []) if isinstance(data, dict) else []
        return [item for item in agents if isinstance(item, dict)]

    # ---------------------------------------------------------------- hosting

    async def create_agent(self, name: str) -> dict[str, Any]:
        data = await self._request("POST", "/v1/hosting/agents", json={"name": name})
        return data if isinstance(data, dict) else {}

    async def upload_code(self, address: str, source: str) -> None:
        body = {"code": source}
        await self._request("PUT", f"/v1/hosting/agents/{address}/code", json=body)

    async def start_agent(self, address: str) -> None:
        await self._request("POST", f"/v1/hosting/agents/{address}/start")

    async def stop_agent(self, address: str) -> None:
        await self._request("POST", f"/v1/hosting/agents/{address}/stop")

    async def delete_agent(self, address: str) -> None:
        await self._request("DELETE", f"/v1/hosting/agents/{address}")

    async def agent_status(self, address: str) -> dict[str, Any]:
        data = await self._request("GET", f"/v1/hosting/agents/{address}")
        return data if isinstance(data, dict) else {}

    async def logs(self, address: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/v1/hosting/agents/{address}/logs")
        items = data.get("logs", []) if isinstance(data, dict) else data
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def update_profile(self, address: str, readme: str, name: str) -> None:
        """Best-effort description/README update used for discovery quality."""
        await self._request(
            "PUT",
            f"/v1/hosting/agents/{address}",
            json={"name": name, "readme": readme},
        )
