"""Cluster counts behind the device / IP / subnet / ASN / velocity signals.

Source of truth: Postgres (`defence.identities`: registration device, IP, subnet, verification time).
Redis is only a 60 s cache of those counts. Flush Redis and the next request recomputes the same numbers
from Postgres (tested). Redis down => straight to Postgres.

Single flight: when 1,500 students behind one campus NAT enter in the same second, the first request
computes the count and the other 1,499 await that one query instead of issuing their own.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from redis.exceptions import RedisError

from .. import metrics
from ..runtime import Runtime

log = logging.getLogger("fd.signals")
CACHE_TTL_S = 60
_inflight: dict[str, asyncio.Future[int]] = {}


async def _cached(rt: Runtime, env: str, key: str, compute: Callable[[], Awaitable[int]]) -> int:
    full = f"fd:{env}:sig:{key}"
    try:
        hit = await rt.redis.get(full)
        if hit is not None:
            return int(hit)
    except (RedisError, OSError, TimeoutError):
        metrics.redis_error("counts_cache")  # cache unavailable: fall through to Postgres

    if full in _inflight:
        return await asyncio.shield(_inflight[full])
    fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    _inflight[full] = fut
    try:
        value = await compute()
        fut.set_result(value)
        try:
            await rt.redis.set(full, value, ex=CACHE_TTL_S)
        except (RedisError, OSError, TimeoutError):
            pass
        return value
    except BaseException as exc:
        fut.set_exception(exc)
        fut.exception()  # mark retrieved so a lone caller does not log "never retrieved"
        raise
    finally:
        _inflight.pop(full, None)


async def _count(rt: Runtime, sql: str, params: tuple) -> int:
    async with rt.pg.connection() as conn:
        row = await (await conn.execute(sql, params)).fetchone()
    return int(row["n"])


async def device_accounts(rt: Runtime, env: str, device_id: str) -> int:
    return await _cached(rt, env, f"dev:{device_id}", lambda: _count(
        rt, "SELECT count(*) AS n FROM defence.identities WHERE device_id = %s", (device_id,)))


async def ip_accounts(rt: Runtime, env: str, ip: str) -> int:
    return await _cached(rt, env, f"ip:{ip}", lambda: _count(
        rt, "SELECT count(*) AS n FROM defence.identities WHERE registration_ip = %s::inet", (ip,)))


async def subnet_accounts(rt: Runtime, env: str, subnet: str) -> int:
    return await _cached(rt, env, f"net:{subnet}", lambda: _count(
        rt, "SELECT count(*) AS n FROM defence.identities WHERE registration_subnet = %s", (subnet,)))


async def asn_accounts(rt: Runtime, env: str, cidr: str) -> int:
    return await _cached(rt, env, f"asn:{cidr}", lambda: _count(
        rt, "SELECT count(*) AS n FROM defence.identities WHERE registration_ip <<= %s::inet", (cidr,)))


async def subnet_burst(rt: Runtime, env: str, subnet: str, verified_at: datetime) -> int:
    """Accounts from the same /24 verified in the same 10-minute bucket as this one (this one excluded).
    A fixed bucket rather than a sliding window so the answer is cacheable and shared by everyone in it."""
    bucket = int(verified_at.timestamp() // 600)

    async def compute() -> int:
        n = await _count(
            rt,
            """SELECT count(*) AS n FROM defence.identities
                WHERE registration_subnet = %s
                  AND verified_at >= to_timestamp(%s) AND verified_at < to_timestamp(%s)""",
            (subnet, bucket * 600, (bucket + 1) * 600),
        )
        return max(0, n - 1)

    return await _cached(rt, env, f"vel:{subnet}:{bucket}", compute)
