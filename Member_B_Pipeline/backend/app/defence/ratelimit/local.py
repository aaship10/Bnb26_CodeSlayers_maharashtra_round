"""Best-effort per-process limiter, used ONLY while Redis is unavailable (`fail_mode: local`).

Same buckets and same all-or-nothing semantics as the Redis limiter, but the state lives in this process, so
with N replicas each one enforces its own copy: the effective global limit during an outage is up to N times
looser. `divisor` (the replica count, FD_REPLICA_COUNT) divides capacity and refill so the cluster-wide total
stays near the configured limit when traffic is spread evenly. It is deliberately weaker than Redis and says
so: it exists so that an outage degrades protection instead of removing it, never to be the real limiter.
Not correctness-critical (allocation never depends on it), so process memory is acceptable here; the table is
bounded and pruned so a flood of distinct keys cannot grow it without limit.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence

from .bucket import Limit, LimitResult


class LocalLimiter:
    def __init__(self, divisor: int = 1, max_keys: int = 50_000, clock: Callable[[], float] = time.monotonic) -> None:
        self.divisor = max(1, divisor)
        self._max, self._clock = max_keys, clock
        self._b: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)

    def _scaled(self, lim: Limit) -> tuple[float, float]:
        return max(1.0, lim.capacity / self.divisor), lim.refill_per_s / self.divisor

    def check(self, limits: Sequence[Limit], cost: int = 1) -> LimitResult:
        now = self._clock()
        state: list[tuple[Limit, float, float, float]] = []
        denied: Limit | None = None
        retry_ms = 0
        for lim in limits:
            cap, rate = self._scaled(lim)
            tokens, ts = self._b.get(lim.key, (cap, now))
            tokens = min(cap, tokens + max(0.0, now - ts) * rate)
            state.append((lim, tokens, cap, rate))
            if tokens < cost:
                denied = denied or lim
                retry_ms = max(retry_ms, math.ceil((cost - tokens) / rate * 1000) if rate > 0 else 60_000)
        if denied is not None:  # all-or-nothing: a rejection charges nothing
            return LimitResult(False, denied.scope, retry_ms, degraded=True)
        for lim, tokens, _cap, _rate in state:
            self._b[lim.key] = (tokens - cost, now)
        if len(self._b) > self._max:
            self._prune(now)
        return LimitResult(True, degraded=True)

    def _prune(self, now: float) -> None:
        # Buckets idle for 10 minutes have long since refilled, so forgetting them loses nothing. If a flood of
        # distinct keys is still too many, drop the least recently used half (they refill to full: more permissive,
        # never an error).
        for k in [k for k, (_, ts) in self._b.items() if now - ts > 600]:
            self._b.pop(k, None)
        if len(self._b) > self._max:
            oldest = sorted(self._b.items(), key=lambda kv: kv[1][1])[: len(self._b) - self._max // 2]
            for k, _ in oldest:
                self._b.pop(k, None)
