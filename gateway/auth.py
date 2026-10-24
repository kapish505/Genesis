"""Capability tokens for generated agents.

A token is stateless so the gateway needs no shared session store:

    g1.<base64url(payload)>.<hmac-sha256 hex>

payload = {
  "a": agent id (the spec name),
  "t": allowed tools,
  "h": allowed fetch hostnames,
  "e": expiry epoch seconds,
  "j": token id (revocable),
}

The gateway trusts only the token: a request cannot widen its own host or tool
scope. Revocation is checked against a small on-disk deny list, written by
Genesis when it deletes an agent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

VERSION = "g1"
ALL_TOOLS = ("fetch", "extract", "search", "llm", "notify")


class TokenError(ValueError):
    pass


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Scope:
    agent_id: str
    tools: frozenset[str]
    hosts: frozenset[str]
    expires_at: float
    token_id: str

    def allows_tool(self, tool: str) -> bool:
        return tool in self.tools

    def allows_host(self, host: str) -> bool:
        return host.casefold().rstrip(".") in self.hosts


def make_token(
    secret: str,
    agent_id: str,
    *,
    tools: list[str] | tuple[str, ...],
    hosts: list[str] | tuple[str, ...],
    ttl_s: int = 90 * 86400,
    token_id: str = "",
) -> str:
    unknown = set(tools) - set(ALL_TOOLS)
    if unknown:
        raise TokenError(f"unknown tools in token scope: {sorted(unknown)}")
    payload: dict[str, Any] = {
        "a": agent_id,
        "t": sorted(set(tools)),
        "h": sorted({host.casefold().rstrip(".") for host in hosts}),
        "e": int(time.time() + ttl_s),
        "j": token_id or hashlib.sha256(f"{agent_id}:{time.time_ns()}".encode()).hexdigest()[:16],
    }
    body = VERSION + "." + _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + _sign(secret, body)


def token_id(token: str, *, secret: str) -> str:
    """Return the revocation id of a valid token (no expiry check, for operators)."""
    return verify_token(token, secret=secret, check_expiry=False).token_id


def decode_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise TokenError("malformed token")
    try:
        raw = json.loads(_b64decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("malformed token payload") from exc
    if not isinstance(raw, dict):
        raise TokenError("malformed token payload")
    return raw


def verify_token(
    token: str,
    *,
    secret: str,
    revoked: frozenset[str] = frozenset(),
    check_expiry: bool = True,
    now: float | None = None,
) -> Scope:
    if not secret or secret == "replace-me":
        raise TokenError("gateway secret is not configured")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise TokenError("malformed token")
    body = f"{parts[0]}.{parts[1]}"
    expected = _sign(secret, body)
    if not hmac.compare_digest(expected, parts[2]):
        raise TokenError("invalid token signature")
    payload = decode_payload(token)
    agent_id = payload.get("a")
    if not isinstance(agent_id, str) or not agent_id:
        raise TokenError("token has no agent id")
    tools = frozenset(str(item) for item in payload.get("t") or [])
    hosts = frozenset(str(item).casefold().rstrip(".") for item in payload.get("h") or [])
    expires_at = float(payload.get("e") or 0)
    jti = str(payload.get("j") or "")
    current = now if now is not None else time.time()
    if check_expiry and expires_at < current:
        raise TokenError("token expired")
    if jti and jti in revoked:
        raise TokenError("token revoked")
    return Scope(agent_id=agent_id, tools=tools, hosts=hosts, expires_at=expires_at, token_id=jti)


def token_digest(token: str) -> str:
    """Stable identifier stored in the ledger without keeping the secret."""
    return hashlib.sha256(token.encode()).hexdigest()
