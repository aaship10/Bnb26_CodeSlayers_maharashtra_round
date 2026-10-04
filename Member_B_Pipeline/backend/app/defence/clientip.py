"""Trusted-proxy client IP resolution and the middleware that publishes it.

Rule: X-Forwarded-For / X-Real-IP are believed ONLY when the TCP peer is in
TRUSTED_PROXIES. Within X-Forwarded-For we walk from the RIGHT and skip trusted
proxies; the first untrusted address is the client. Walking from the left would
let any client choose its own address by prepending a fake entry.

Run uvicorn with --no-proxy-headers: otherwise uvicorn rewrites the peer address
itself (for 127.0.0.1 by default) before this code sees it.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from .settings import IPNetwork, get_settings
from .simheaders import sim_authorized

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class ClientInfo:
    ip: str
    subnet: str  # IPv4 /24, IPv6 /64: the unit of "same network" for soft signals
    source: str  # peer | forwarded | real_ip | sim


def parse_ip(raw: str | None) -> IPAddress | None:
    if not raw:
        return None
    raw = raw.strip()
    # tolerate "[::1]:1234" and "1.2.3.4:5678" forms some proxies emit
    if raw.startswith("[") and "]" in raw:
        raw = raw[1 : raw.index("]")]
    elif raw.count(":") == 1 and "." in raw:
        raw = raw.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped  # ::ffff:1.2.3.4 is the same host as 1.2.3.4
    return ip


def subnet_of(ip: IPAddress) -> str:
    prefix = 24 if ip.version == 4 else 64
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


def _trusted(ip: IPAddress, proxies: tuple[IPNetwork, ...]) -> bool:
    return any(ip.version == p.version and ip in p for p in proxies)


def resolve_client(
    peer: str | None,
    headers: Mapping[str, str],
    trusted_proxies: tuple[IPNetwork, ...],
    sim_ok: bool = False,
) -> ClientInfo:
    """`headers` must be case-insensitive (starlette Headers) or already lower-cased."""
    peer_ip = parse_ip(peer) or ipaddress.ip_address("0.0.0.0")  # unknown peer: one shared bucket

    if sim_ok:
        sim_ip = parse_ip(headers.get("x-sim-client-ip"))
        if sim_ip is not None:
            return ClientInfo(str(sim_ip), subnet_of(sim_ip), "sim")

    if _trusted(peer_ip, trusted_proxies):
        chain = [parse_ip(p) for p in (headers.get("x-forwarded-for") or "").split(",")]
        for hop in reversed([c for c in chain if c is not None]):
            if not _trusted(hop, trusted_proxies):
                return ClientInfo(str(hop), subnet_of(hop), "forwarded")
        real = parse_ip(headers.get("x-real-ip"))
        if real is not None and not _trusted(real, trusted_proxies):
            return ClientInfo(str(real), subnet_of(real), "real_ip")
    return ClientInfo(str(peer_ip), subnet_of(peer_ip), "peer")


class ClientIPMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so streaming/SSE responses are untouched.
    Publishes request.state.client_ip and request.state.client_subnet."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = Headers(scope=scope)
            peer = scope["client"][0] if scope.get("client") else None
            info = resolve_client(peer, headers, get_settings().trusted_proxies, sim_authorized(headers))
            state = scope.setdefault("state", {})
            state["client_ip"] = info.ip
            state["client_subnet"] = info.subnet
            state["client_ip_source"] = info.source
        await self.app(scope, receive, send)
