"""SSRF defence for the gateway's fetch tool.

Layered checks, all of which must pass:

1. scheme is http/https, no userinfo, sane port;
2. the hostname is on the token's allow list (exact match, no suffix matching);
3. every DNS answer is a public unicast address (no private, loopback,
   link-local, multicast, reserved, unspecified, NAT64 or IPv4-mapped space);
4. the request connects to the *validated IP*, not a re-resolved hostname, with
   the original name sent as SNI and Host. This closes the DNS-rebinding window
   between validation and connection;
5. redirects are followed manually, at most ``MAX_REDIRECTS``, and each hop is
   re-validated from step 1;
6. the response body is streamed with a byte cap and a content-type allow list.

``validate_url`` alone is the pure, network-using validator used by tests; the
fetch implementation lives in ``gateway.main`` and reuses it per hop.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "application/xhtml+xml",
    "application/json",
    "application/pdf",
    "text/xml",
    "application/xml",
)

# NAT64 well-known prefix and other ranges that ``ipaddress.is_private`` misses
# on some Python versions.
EXTRA_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "64:ff9b::/96",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "198.18.0.0/15",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


class SSRFError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int
    ip: str

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"

    def connect_url(self) -> str:
        """URL that dials the validated IP while keeping path and query."""
        parts = urlsplit(self.url)
        netloc = f"[{self.ip}]" if ":" in self.ip else self.ip
        netloc = f"{netloc}:{self.port}"
        return urlunsplit((self.scheme, netloc, parts.path or "/", parts.query, ""))


def _address_allowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _address_allowed(address.ipv4_mapped)
        if address.sixtofour is not None:
            return False
        if address.teredo is not None:
            return False
    if address.is_private or address.is_loopback or address.is_link_local:
        return False
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return False
    if isinstance(address, ipaddress.IPv4Address) and address.is_global is False:
        return False
    return not any(address in network for network in EXTRA_BLOCKED_NETWORKS)


def resolve_host(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFError("host did not resolve") from exc
    addresses = []
    for info in infos:
        address = str(info[4][0]).split("%")[0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise SSRFError("host did not resolve")
    return addresses


def validate_url(url: str, allowed_hosts: set[str] | None = None) -> ValidatedURL:
    if not url or len(url) > 2048:
        raise SSRFError("URL is empty or too long")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise SSRFError("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise SSRFError("URLs with credentials are not allowed")
    host = (parts.hostname or "").casefold().rstrip(".")
    if not host:
        raise SSRFError("URL has no host")
    if host == "localhost" or host.endswith(".localhost"):
        raise SSRFError("loopback host is blocked")
    if allowed_hosts is not None and host not in {
        item.casefold().rstrip(".") for item in allowed_hosts
    }:
        raise SSRFError(f"host '{host}' is not allowed for this agent")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise SSRFError(f"port {port} is not allowed")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = resolve_host(host, port)
    else:
        addresses = [str(literal)]

    chosen: str | None = None
    for value in addresses:
        try:
            parsed_address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise SSRFError("host resolved to an invalid address") from exc
        if not _address_allowed(parsed_address):
            raise SSRFError("private or special-use address is blocked")
        if chosen is None:
            chosen = str(parsed_address)
    if chosen is None:
        raise SSRFError("host did not resolve")
    return ValidatedURL(url=url, scheme=parts.scheme, host=host, port=port, ip=chosen)


def is_allowed_content_type(content_type: str | None) -> bool:
    if not content_type:
        return True
    base = content_type.split(";")[0].strip().casefold()
    return base.startswith(tuple(ALLOWED_CONTENT_TYPES))
