"""Process-wide connections used by the defence package: Postgres pool, Redis client,
rate limiter, mail sender.

These are connections, not state: nothing correctness-critical lives here. They are
created lazily on first use (so unit tests and A's startup order do not matter) and
closed from the app lifespan (see install()).
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .errors import ApiError, ErrorCode
from . import metrics
from .decisionlog.writer import DecisionLog
from .resilience import Breaker, GuardedRedis
from .identity.mail import MailSender, build_sender
from .ratelimit.bucket import RedisLimiter
from .settings import get_settings

log = logging.getLogger("fd.runtime")


@dataclass
class Runtime:
    pg: AsyncConnectionPool
    redis: Any
    limiter: RedisLimiter
    mailer: MailSender
    decisions: DecisionLog | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)


_rt: Runtime | None = None
_lock: asyncio.Lock | None = None


async def get_runtime() -> Runtime:
    global _rt, _lock
    if _rt is not None:
        return _rt
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _rt is None:
            _rt = await _build()
    return _rt


def make_redis(url: str, max_connections: int = 100):
    """Redis client for the limiter.

    * protocol=2: redis-py 8 defaults to RESP3 and opens with `HELLO 3`, which Redis < 6 rejects
      ("unknown command HELLO"). RESP2 works against every Redis version we might meet.
    * BlockingConnectionPool: when all connections are busy, callers WAIT (up to 0.5 s) instead of
      raising. The plain pool raises MaxConnectionsError under a burst, and because the limiter
      fails open that would switch the defence off exactly when the crowd arrives.
    * short socket timeouts: a dead Redis must cost the hot path milliseconds, not seconds.
    """
    pool = aioredis.BlockingConnectionPool.from_url(
        url, max_connections=max_connections, timeout=0.5, decode_responses=True,
        socket_timeout=0.5, socket_connect_timeout=0.5, protocol=2,
    )
    client = GuardedRedis(connection_pool=pool)
    # After N consecutive connection failures stop paying the socket timeout on every request (see resilience.py).
    client.breaker = Breaker(int(os.environ.get("REDIS_BREAKER_THRESHOLD", "5")), float(os.environ.get("REDIS_BREAKER_COOLDOWN_S", "3")))
    return client


async def _build() -> Runtime:
    s = get_settings()
    if not s.database_url:
        raise ApiError(ErrorCode.INTERNAL, "DATABASE_URL is not configured", status=503)
    pg = AsyncConnectionPool(
        s.database_url,
        min_size=1,
        max_size=int(os.environ.get("DEFENCE_POOL_SIZE", "4")),
        kwargs={"row_factory": dict_row},
        check=AsyncConnectionPool.check_connection,  # a socket Postgres killed is replaced BEFORE it is handed out, not discovered by a failed request
        timeout=5,  # a query waits at most 5 s for a connection, then fails: no 30 s hangs in an outage
        open=False,
    )
    # wait=False: a Postgres outage must not also take down the Redis-backed rate limiter, which
    # shares this Runtime. Connections are established in the background; queries fail fast until then.
    await pg.open(wait=False)
    # Short timeouts: a dead Redis must cost the hot path milliseconds, not seconds.
    r = make_redis(s.redis_url)
    rt = Runtime(pg=pg, redis=r, limiter=RedisLimiter(r, s.env), mailer=build_sender(s))
    await rt.limiter.warm()
    rt.decisions = DecisionLog(pg)
    rt.decisions.start()
    metrics.bind_decision_log(rt.decisions)
    rt.tasks.append(asyncio.create_task(_janitor(rt), name="fd-janitor"))
    return rt


async def _janitor(rt: Runtime) -> None:
    """Best-effort cleanup of expired OTP rows. Several replicas may run it: harmless."""
    while True:
        try:
            await asyncio.sleep(300)
            async with rt.pg.connection() as conn:
                await conn.execute(
                    "DELETE FROM defence.pending_registrations WHERE expires_at < now() - interval '1 hour'"
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("janitor iteration failed")


async def startup() -> None:
    """Called from the app lifespan: migrate (advisory-locked) then open connections."""
    s = get_settings()
    if not s.database_url:
        log.warning("DATABASE_URL not set: defence identity features are unavailable")
        return
    if s.auto_migrate:
        from .migrate import upgrade

        await asyncio.to_thread(upgrade, s.database_url)
    await get_runtime()


async def shutdown() -> None:
    global _rt
    rt, _rt = _rt, None
    if rt is None:
        return
    for t in rt.tasks:
        t.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await t
    if rt.decisions is not None:
        with suppress(Exception):
            await rt.decisions.stop()  # flush what is queued; a crash would lose it (documented)
    with suppress(Exception):
        await rt.redis.aclose()
        await rt.redis.connection_pool.disconnect()
    with suppress(Exception):
        await rt.pg.close()


def reset_for_tests() -> None:
    """Forget the runtime without closing (the owning event loop may be gone)."""
    global _rt, _lock
    _rt, _lock = None, None
