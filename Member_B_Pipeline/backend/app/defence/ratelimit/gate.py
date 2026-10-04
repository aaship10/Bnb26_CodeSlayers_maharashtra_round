"""Per-endpoint application rate limiting, driven by events.config.defences.rate_limit.

Used as a route dependency:   @app.post("/events/{event_id}/enter", dependencies=[Depends(rate_limit("enter"))])
It runs BEFORE authentication and before any database work of the route:
  * identity comes from the stateless JWT (no lookup), so shedding costs one Redis round trip;
  * the event's config is cached (config_source), so Postgres is not on this path;
  * a request with no/invalid token is still limited by ip / subnet / device.
Disabled layer or unconfigured endpoint => returns immediately, zero Redis calls.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request

from ..clientip import resolve_client
from ..config_schema import DefencesConfig
from ..config_source import get_defences
from ..errors import ApiError, ErrorCode
from ..identity.deps import peek_user_id
from ..runtime import get_runtime
from ..settings import get_settings
from .. import metrics
from ..signals import timing
from ..simheaders import sim_authorized
from .bucket import Limit
from .errors import rate_limited

log = logging.getLogger("fd.ratelimit")


def _client_ip(request: Request) -> tuple[str, str]:
    st = request.state
    ip, subnet = getattr(st, "client_ip", None), getattr(st, "client_subnet", None)
    if ip is None:  # middleware not installed
        peer = request.client.host if request.client else None
        info = resolve_client(peer, request.headers, get_settings().trusted_proxies, sim_authorized(request.headers))
        return info.ip, info.subnet
    return ip, subnet


def _device(request: Request) -> str | None:
    raw = request.headers.get("x-device-id")
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))  # client-chosen header: only well-formed UUIDs count
    except ValueError:
        return None


async def enforce(request: Request, endpoint: str, event_id: uuid.UUID | str, cfg: DefencesConfig | None = None) -> None:
    cfg = cfg or await get_defences(event_id)
    if cfg.layers.signals.enabled and endpoint in ("enter", "challenge"):
        # Timing-regularity evidence, recorded BEFORE the limiter decides (a hammering client's rejected
        # requests still count) and independent of whether rate limiting itself is on.
        who = peek_user_id(request)
        if who is not None:
            await timing.record((await get_runtime()).redis, get_settings().env, str(event_id), str(who))
    rl = cfg.layers.rate_limit
    if not rl.enabled:
        return
    buckets = rl.limits.get(endpoint)  # type: ignore[call-overload]
    if not buckets:
        return

    ip, subnet = _client_ip(request)
    user = peek_user_id(request)
    ids = {"ip": ip, "subnet": subnet, "identity": str(user) if user else None, "device": _device(request)}
    prefix = f"{event_id}:{endpoint}"
    limits = [
        Limit(dim, f"{prefix}:{dim}:{ids[dim]}", b.capacity, b.refill_per_s)
        for dim, b in buckets.items()
        if ids.get(dim)
    ]

    rt = await get_runtime()
    res = await rt.limiter.check_policy(limits, cfg.fail_mode)
    if res.degraded and cfg.fail_mode == "closed":
        # Redis is down and the organiser chose to protect the database over availability.
        raise ApiError(
            ErrorCode.INTERNAL, "temporarily unavailable", status=503, headers={"Retry-After": "2"},
            details={"retry_after_ms": 2000, "scope": "limiter"},
        )
    if not res.allowed:
        metrics.RATE_LIMITED.labels(endpoint, res.scope or "unknown").inc()
        raise rate_limited(res, rl.retry_jitter_max)


def rate_limit(endpoint: str) -> Callable[[Request], Awaitable[None]]:
    """Dependency factory for routes with an {event_id} path parameter."""

    async def dependency(request: Request) -> None:
        raw = request.path_params.get("event_id")
        try:
            event_id = uuid.UUID(str(raw))
        except ValueError:
            return  # malformed id: the route's own validation answers 422; nothing to limit per event
        await enforce(request, endpoint, event_id)

    return dependency
