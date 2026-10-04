"""Multi-dimension token-bucket limiter: ONE Redis round trip, atomic, all-or-nothing.

Why a Lua script: the check across ip / subnet / identity / device buckets and the
deduction must be one atomic step. With separate calls two concurrent requests can
both see "1 token left" and both pass, or one dimension can be charged while another
rejects. Inside the script nothing else touches Redis, so neither can happen.

Design choices worth knowing:
* Time comes from Redis (TIME), not the app server: replicas with skewed clocks all
  see one clock.
* A rejected request deducts nothing and writes nothing, so a flood from one
  attacker does not burn Redis write throughput and cannot drain other dimensions.
* Keys expire after the time a bucket needs to refill completely (+1 s), so idle
  identities cost no memory: Redis is a rebuildable cache.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

from redis.exceptions import RedisError

from .. import metrics
from ..simheaders import warn_throttled

LUA ="""
pcall(redis.replicate_commands)
local t = redis.call('TIME')
local now_ms = t[1] * 1000 + math.floor(t[2] / 1000)
local cost = tonumber(ARGV[1])
local n = #KEYS
local allowed, denied, retry_ms = 1, 0, 0
local tokens_after = {}
for i = 1, n do
  local cap = tonumber(ARGV[2 * i])
  local rate = tonumber(ARGV[2 * i + 1])
  local h = redis.call('HMGET', KEYS[i], 'tk', 'ts')
  local tokens = tonumber(h[1])
  local ts = tonumber(h[2])
  if tokens == nil then tokens = cap; ts = now_ms end
  local elapsed = now_ms - ts
  if elapsed < 0 then elapsed = 0 end
  tokens = math.min(cap, tokens + elapsed * rate / 1000)
  tokens_after[i] = tokens
  if tokens < cost then
    allowed = 0
    if denied == 0 then denied = i end
    local wait = math.ceil((cost - tokens) / rate * 1000)
    if wait > retry_ms then retry_ms = wait end
  end
end
if allowed == 1 then
  for i = 1, n do
    local cap = tonumber(ARGV[2 * i])
    local rate = tonumber(ARGV[2 * i + 1])
    redis.call('HSET', KEYS[i], 'tk', tostring(tokens_after[i] - cost), 'ts', tostring(now_ms))
    redis.call('PEXPIRE', KEYS[i], math.ceil(cap / rate * 1000) + 1000)
  end
end
return {allowed, denied, retry_ms}
"""

log = logging.getLogger("fd.ratelimit")


@dataclass(frozen=True)
class Limit:
    scope: str  # "ip" | "subnet" | "identity" | "device" | "email": reported in 429 details
    key: str  # unique bucket id inside the namespace, e.g. "register:ip:203.0.113.7"
    capacity: float
    refill_per_s: float


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    scope: str | None = None  # first denied scope
    retry_after_ms: int = 0
    degraded: bool = False  # True when Redis was unavailable and we failed open


def hash_id(value: str) -> str:
    """Opaque bucket id for personal data (emails): keeps PII out of Redis keys."""
    return hashlib.sha256(value.encode()).hexdigest()[:24]


class RedisLimiter:
    def __init__(self, redis, env: str = "dev", local: "LocalLimiter | None" = None) -> None:
        from .local import LocalLimiter  # lazy: local.py imports this module's Limit/LimitResult

        self._redis = redis
        self.local = local or LocalLimiter(divisor=int(os.environ.get("FD_REPLICA_COUNT", "1") or 1))
        self._prefix = f"fd:{env}:rl:"
        self._script = redis.register_script(LUA)

    async def warm(self) -> None:
        """Load the script into Redis' script cache up front. Saves the NOSCRIPT round trip on
        the first request, and avoids an error reply on the very first call (fakeredis' TCP
        server drops the connection after an error reply). Best effort: redis-py still reloads
        transparently if Redis later restarts and forgets the script."""
        try:
            await self._redis.script_load(LUA)
        except (RedisError, OSError, TimeoutError) as exc:
            # Loud on purpose: because check_or_open() fails OPEN, a limiter that cannot talk to
            # Redis (wrong version, auth, network) is otherwise invisible in API responses.
            metrics.redis_error("limiter")
            log.error("RATE LIMITER CANNOT USE REDIS (%s); the fail_mode policy applies until fixed", exc)

    async def check(self, limits: Sequence[Limit], cost: int = 1) -> LimitResult:
        """Raises redis.RedisError / OSError if Redis is unreachable; use check_or_open()."""
        if not limits:
            return LimitResult(True)
        for lim in limits:
            if cost > lim.capacity:
                raise ValueError(f"cost {cost} exceeds capacity of bucket {lim.key}: it could never pass")
        keys = [self._prefix + lim.key for lim in limits]
        args: list[float | int] = [cost]
        for lim in limits:
            args += [lim.capacity, lim.refill_per_s]
        allowed, denied, retry_ms = await self._script(keys=keys, args=args)
        if int(allowed) == 1:
            return LimitResult(True)
        return LimitResult(False, limits[int(denied) - 1].scope, int(retry_ms))

    async def check_policy(self, limits: Sequence[Limit], fail_mode: str = "local", cost: int = 1) -> LimitResult:
        """Check Redis; if Redis is unusable apply `fail_mode` (config: defences.fail_mode):

          local   a best-effort per-process limiter (default): protection degrades, it does not vanish
          open    let everything through
          closed  refuse (denied, scope "limiter") so the database is protected at the cost of availability

        The result is flagged `degraded` in every failure case. Allocation never depends on this."""
        try:
            return await self.check(limits, cost)
        except (RedisError, OSError, TimeoutError) as exc:
            metrics.redis_error("limiter")
            metrics.LIMITER_DEGRADED.labels(fail_mode).inc()
            warn_throttled("limiter-degraded", f"rate limiter cannot use Redis ({exc!r}); applying fail_mode={fail_mode}", every_s=10)
            if fail_mode == "local":
                return self.local.check(limits, cost)
            if fail_mode == "closed":
                return LimitResult(False, "limiter", 2000, degraded=True)
            return LimitResult(True, degraded=True)

    async def check_or_open(self, limits: Sequence[Limit], cost: int = 1) -> LimitResult:
        """Fail-open convenience kept for callers that want exactly that."""
        return await self.check_policy(limits, "open", cost)
