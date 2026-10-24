"""Relay to existing agents over ACP.

Edge cases covered:

* no reply within ``timeout`` → retry once with an explicit ``StartSessionContent``;
* a reply that arrives after the timeout is discarded, not mis-delivered;
* errors raised while sending are surfaced as ``DelegationError``;
* only text content is returned; cards from other agents are flattened to text so
  Genesis never renders a nested interactive card.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
)


class DelegationError(RuntimeError):
    def __init__(self, message: str, reason: str = "no_reply") -> None:
        super().__init__(message)
        self.reason = reason


class ReplyRouter:
    """Correlates replies to the request that caused them.

    A single ChatMessage callback funnels every inbound message here. Keyed by
    sender address, with a FIFO of waiters so two concurrent requests to the same
    agent cannot steal each other's replies.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, list[asyncio.Future[str]]] = {}

    def expect(self, address: str) -> asyncio.Future[str]:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(address, []).append(future)
        return future

    def deliver(self, address: str, text: str) -> bool:
        queue = self._waiters.get(address)
        if not queue:
            return False
        future = queue.pop(0)
        if not queue:
            self._waiters.pop(address, None)
        if not future.done():
            future.set_result(text)
        return True

    def cancel(self, address: str, future: asyncio.Future[str]) -> None:
        queue = self._waiters.get(address)
        if queue and future in queue:
            queue.remove(future)
        if queue is not None and not queue:
            self._waiters.pop(address, None)


SendFn = Callable[[str, ChatMessage], Awaitable[None]]


def _text_of(message: ChatMessage) -> str:
    parts = [item.text for item in message.content if isinstance(item, TextContent)]
    return "\n".join(part for part in parts if part).strip()


class Relay:
    def __init__(
        self,
        send: SendFn,
        router: ReplyRouter,
        *,
        default_timeout_s: float = 45.0,
    ) -> None:
        self.send = send
        self.router = router
        self.default_timeout_s = default_timeout_s

    async def ask(
        self,
        address: str,
        text: str,
        *,
        timeout_s: float | None = None,
        session_id: str = "",
    ) -> str:
        timeout = timeout_s or self.default_timeout_s
        first = await self._attempt(address, text, timeout, start_session=False)
        if first is not None:
            return first
        second = await self._attempt(address, text, timeout, start_session=True)
        if second is not None:
            return second
        raise DelegationError(
            f"the agent at {address} did not reply within {int(timeout)}s",
            reason="timeout",
        )

    async def _attempt(
        self, address: str, text: str, timeout: float, *, start_session: bool
    ) -> str | None:
        future = self.router.expect(address)
        content: list[Any] = []
        if start_session:
            content.append(StartSessionContent(type="start-session"))
        content.append(TextContent(type="text", text=text))
        message = ChatMessage(content=content)
        try:
            await self.send(address, message)
        except Exception as exc:  # noqa: BLE001 - surface any transport failure
            self.router.cancel(address, future)
            raise DelegationError(
                f"could not send to {address}: {type(exc).__name__}", "send"
            ) from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self.router.cancel(address, future)
            return None

    @staticmethod
    def end_session() -> ChatMessage:
        return ChatMessage(content=[EndSessionContent(type="end-session")])


def request_id() -> str:
    return uuid.uuid4().hex
