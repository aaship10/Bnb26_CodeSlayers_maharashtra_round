"""Redis failure handling: circuit breaker + a client that applies it to every call.

Why a breaker at all: with Redis down, each call would otherwise wait for its socket timeout (0.5 s) before
failing open/local. During a flash crowd that turns "Redis is down" into "every request is 500 ms slower",
which is a worse outage than the one we are surviving. After `threshold` consecutive connection failures the
breaker opens: calls fail IMMEDIATELY (microseconds) with a ConnectionError that all existing handlers already
treat as "Redis unavailable". After `cooldown_s` one probe call is allowed (half-open); success closes it.

Only connection-level failures count. A reply like NOSCRIPT/WRONGTYPE proves Redis is alive.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError, TimeoutError as RedisTimeoutError

from . import metrics

log = logging.getLogger("fd.resilience")


class Breaker:
    def __init__(self, threshold: int = 5, cooldown_s: float = 3.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.cooldown_s, self._clock = threshold, cooldown_s, clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self._opened_at >= self.cooldown_s else "open"

    def allow(self) -> bool:
        s = self.state
        if s == "closed":
            return True
        if s == "half_open" and not self._probing:
            self._probing = True  # exactly one caller probes; the rest keep failing fast
            return True
        return False

    def success(self) -> None:
        if self._opened_at is not None:
            log.warning("redis circuit CLOSED: Redis is reachable again")
        self._failures, self._opened_at, self._probing = 0, None, False
        metrics.BREAKER_OPEN.set(0)

    def failure(self) -> None:
        self._failures += 1
        was_closed = self._opened_at is None
        if self._probing or self._failures >= self.threshold:
            self._opened_at = self._clock()  # (re)start the cool-down
            self._probing = False
            metrics.BREAKER_OPEN.set(1)
            if was_closed:
                metrics.BREAKER_TRIPS.inc()
                log.error("redis circuit OPEN after %d consecutive failures: skipping Redis for %.1fs", self._failures, self.cooldown_s)


_DOWN = (RedisConnectionError, RedisTimeoutError, OSError, TimeoutError)


class GuardedRedis(aioredis.Redis):
    """redis.asyncio.Redis whose commands and pipelines go through a Breaker (set `.breaker`)."""

    breaker: Breaker | None = None

    async def _guarded(self, call):
        b = self.breaker
        if b is None:
            return await call()
        if not b.allow():
            raise RedisConnectionError("redis circuit open")
        try:
            result = await call()
        except ResponseError:
            b.success()  # the server answered: it is up, the command was just refused
            raise
        except _DOWN:
            b.failure()
            raise
        b.success()
        return result

    async def execute_command(self, *args, **options):
        return await self._guarded(lambda: super(GuardedRedis, self).execute_command(*args, **options))

    def pipeline(self, transaction: bool = True, shard_hint=None):
        p = super().pipeline(transaction, shard_hint)
        original = p.execute

        async def execute(raise_on_error: bool = True):
            return await self._guarded(lambda: original(raise_on_error))

        p.execute = execute  # type: ignore[method-assign]
        return p
