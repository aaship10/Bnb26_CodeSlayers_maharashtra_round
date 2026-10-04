"""Per-identity request timestamps for the timing-regularity signal.

Recorded for the endpoints a bot loops on (/enter and /challenge), never for /status (the honest
frontend polls it periodically on purpose). Redis list, newest first, capped at 20, expires after an
hour. It is a rebuildable cache: losing it only means the signal says "not enough samples".
Recorded BEFORE the rate-limit decision, so a hammering client's rejected requests still count.
"""
from __future__ import annotations

import logging
import time

from redis.exceptions import RedisError

from .. import metrics

log = logging.getLogger("fd.signals")
KEEP = 20
TTL_S = 3600


def _key(env: str, event_id: str, user_id: str) -> str:
    return f"fd:{env}:timing:{event_id}:{user_id}"


async def record(redis, env: str, event_id: str, user_id: str, now_ms: int | None = None) -> None:
    key = _key(env, event_id, user_id)
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        async with redis.pipeline(transaction=False) as p:
            p.lpush(key, ts)
            p.ltrim(key, 0, KEEP - 1)
            p.expire(key, TTL_S)
            await p.execute()
    except (RedisError, OSError, TimeoutError) as exc:
        metrics.redis_error("timing")
        log.debug("timing record skipped: %r", exc)


async def intervals_ms(redis, env: str, event_id: str, user_id: str) -> list[float]:
    try:
        raw = await redis.lrange(_key(env, event_id, user_id), 0, KEEP - 1)
    except (RedisError, OSError, TimeoutError):
        metrics.redis_error("timing")
        return []
    ts = sorted(int(x) for x in raw)  # oldest first
    return [float(b - a) for a, b in zip(ts, ts[1:])]
