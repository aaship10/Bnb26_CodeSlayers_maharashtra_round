"""Current load estimate for adaptive difficulty: gate calls per second, per event, cluster-wide.

One Redis pipeline (INCR this second's counter, GET the previous second's) per gate call. The
previous COMPLETE second is used so the estimate does not jump while a second is half-counted.
Redis failure => load 0.0 (no extra difficulty): load shaping must never block entry.
"""
from __future__ import annotations

import logging
import time

from redis.exceptions import RedisError

from .. import metrics

log = logging.getLogger("fd.load")


async def record_and_get(redis, env: str, event_id: str, ref_rps: int, now: float | None = None) -> float:
    sec = int(time.time() if now is None else now)
    cur, prev = f"fd:{env}:load:{event_id}:{sec}", f"fd:{env}:load:{event_id}:{sec - 1}"
    try:
        async with redis.pipeline(transaction=False) as p:
            p.incr(cur)
            p.expire(cur, 5)
            p.get(prev)
            _, _, prev_count = await p.execute()
    except (RedisError, OSError, TimeoutError) as exc:
        metrics.redis_error("load")
        log.warning("load estimate unavailable (%r); using 0", exc)
        return 0.0
    return min(1.0, int(prev_count or 0) / max(1, ref_rps))
