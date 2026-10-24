"""Tavily-backed web search for the gateway.

Used by generated agents' ``search`` step. The gateway owns the API key; the
agent only ever sees result text.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

logger = logging.getLogger(__name__)

MAX_RESULTS = 5


class SearchError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) or (
        isinstance(exc, httpx.HTTPStatusError)
        and (exc.response.status_code == 429 or exc.response.status_code >= 500)
    )


class TavilySearch:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, max_results: int = 3) -> list[dict[str, Any]]:
        if not self.configured:
            raise SearchError("search is not configured on this gateway")
        count = max(1, min(MAX_RESULTS, int(max_results or 3)))
        body = {
            "api_key": self.api_key,
            "query": query[:400],
            "search_depth": "basic",
            "max_results": count,
            "include_answer": False,
            "include_raw_content": False,
        }

        @retry(
            reraise=True,
            stop=stop_after_attempt(3),
            wait=wait_exponential_jitter(initial=1, max=6),
            retry=retry_if_exception(_retryable),
        )
        async def _call() -> dict[str, Any]:
            response = await self._http().post(f"{self.base_url}/search", json=body)
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

        try:
            payload = await _call()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise SearchError(
                f"search provider returned HTTP {status}",
                retryable=status == 429 or status >= 500,
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SearchError("search provider is unreachable", retryable=True) from exc
        except ValueError as exc:
            raise SearchError("search provider returned invalid JSON") from exc

        results = payload.get("results") or []
        cleaned: list[dict[str, Any]] = []
        for item in results[:count]:
            if not isinstance(item, dict):
                continue
            cleaned.append(
                {
                    "title": str(item.get("title", ""))[:200],
                    "url": str(item.get("url", ""))[:1000],
                    "content": str(item.get("content", ""))[:4000],
                }
            )
        return cleaned
