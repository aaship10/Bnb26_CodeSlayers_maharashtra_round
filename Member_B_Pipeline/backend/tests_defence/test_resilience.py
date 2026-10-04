"""Redis failure handling: breaker state machine, guarded client, local fallback limiter, fail-mode policy."""
import time

import pytest
import redis.asyncio as aioredis
from app.defence import metrics
from app.defence.metrics import REGISTRY
from app.defence.ratelimit.bucket import Limit, RedisLimiter
from app.defence.ratelimit.local import LocalLimiter
from app.defence.resilience import Breaker, GuardedRedis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError


def val(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------- the breaker
def test_breaker_opens_after_the_threshold_and_not_before():
    c = Clock()
    b = Breaker(threshold=3, cooldown_s=5, clock=c)
    for _ in range(2):
        b.failure()
        assert b.state == "closed" and b.allow()
    b.failure()
    assert b.state == "open" and not b.allow()


def test_a_success_resets_the_failure_streak():
    b = Breaker(threshold=3, clock=Clock())
    b.failure(), b.failure(), b.success(), b.failure(), b.failure()
    assert b.state == "closed"  # failures must be CONSECUTIVE


def test_half_open_allows_exactly_one_probe_then_decides():
    c = Clock()
    b = Breaker(threshold=1, cooldown_s=5, clock=c)
    b.failure()
    c.t += 4.9
    assert b.state == "open" and not b.allow()
    c.t += 0.2
    assert b.state == "half_open"
    assert b.allow() is True and b.allow() is False  # one probe at a time; everyone else still fails fast
    b.failure()  # the probe failed: back to open, fresh cool-down
    assert b.state == "open" and not b.allow()
    c.t += 5.1
    assert b.allow() is True
    b.success()
    assert b.state == "closed" and b.allow()


def test_breaker_updates_its_metrics():
    c = Clock()
    trips0 = val("fd_redis_breaker_trips_total")
    b = Breaker(threshold=1, cooldown_s=1, clock=c)
    b.failure()
    assert val("fd_redis_breaker_open") == 1 and val("fd_redis_breaker_trips_total") == trips0 + 1
    b.failure()  # already open: not a new trip
    assert val("fd_redis_breaker_trips_total") == trips0 + 1
    c.t += 2
    b.allow(), b.success()
    assert val("fd_redis_breaker_open") == 0


# ------------------------------------------------------------------------------ the guarded client
def make_guarded(threshold=2, cooldown=5, clock=None):
    r = GuardedRedis(connection_pool=aioredis.BlockingConnectionPool.from_url("redis://127.0.0.1:1/0", protocol=2, timeout=0.1))
    r.breaker = Breaker(threshold, cooldown, clock or Clock())
    return r


async def test_after_the_threshold_calls_fail_instantly_without_touching_the_network(monkeypatch):
    calls = []

    async def down(self, *a, **k):
        calls.append(a)
        raise RedisConnectionError("refused")

    monkeypatch.setattr(aioredis.Redis, "execute_command", down)
    r = make_guarded(threshold=2)
    for _ in range(2):
        with pytest.raises(RedisConnectionError):
            await r.get("k")
    assert len(calls) == 2 and r.breaker.state == "open"
    start = time.perf_counter()
    for _ in range(100):
        with pytest.raises(RedisConnectionError, match="circuit open"):
            await r.get("k")
    assert len(calls) == 2  # not one more network attempt
    assert (time.perf_counter() - start) / 100 < 0.001  # microseconds, not a 500 ms socket timeout


async def test_probe_after_cooldown_closes_the_circuit_when_redis_is_back(monkeypatch):
    state = {"up": False}

    async def maybe(self, *a, **k):
        if not state["up"]:
            raise RedisConnectionError("down")
        return b"ok"

    monkeypatch.setattr(aioredis.Redis, "execute_command", maybe)
    c = Clock()
    r = make_guarded(threshold=1, cooldown=5, clock=c)
    with pytest.raises(RedisConnectionError):
        await r.get("k")
    with pytest.raises(RedisConnectionError, match="circuit open"):
        await r.get("k")
    state["up"] = True
    c.t += 6
    assert await r.get("k") == b"ok" and r.breaker.state == "closed"


async def test_a_refused_command_does_not_count_as_redis_being_down(monkeypatch):
    async def refuse(self, *a, **k):
        raise ResponseError("NOSCRIPT No matching script")

    monkeypatch.setattr(aioredis.Redis, "execute_command", refuse)
    r = make_guarded(threshold=1)
    for _ in range(5):
        with pytest.raises(ResponseError):
            await r.get("k")
    assert r.breaker.state == "closed"  # the server answered; it is alive


async def test_pipelines_are_guarded_too(monkeypatch):
    async def down(self, raise_on_error=True):
        raise RedisConnectionError("down")

    monkeypatch.setattr(aioredis.client.Pipeline, "execute", down)
    r = make_guarded(threshold=1)
    with pytest.raises(RedisConnectionError):
        async with r.pipeline(transaction=False) as p:
            p.get("a")
            await p.execute()
    assert r.breaker.state == "open"
    with pytest.raises(RedisConnectionError, match="circuit open"):
        async with r.pipeline(transaction=False) as p:
            p.get("a")
            await p.execute()


async def test_unreachable_host_really_costs_only_the_first_few_calls():
    """No mocks: a documentation-range address blackholes the connection (or is refused quickly, depending on the
    network). Either way the third call must be instant because the breaker is open."""
    r = GuardedRedis(connection_pool=aioredis.BlockingConnectionPool.from_url(
        "redis://203.0.113.1:6379/0", protocol=2, timeout=0.3, socket_connect_timeout=0.3, socket_timeout=0.3))
    r.breaker = Breaker(2, 30)
    for _ in range(2):
        try:
            await r.get("k")
        except Exception:
            pass
    start = time.perf_counter()
    with pytest.raises(RedisConnectionError, match="circuit open"):
        await r.get("k")
    assert time.perf_counter() - start < 0.05
    await r.connection_pool.disconnect()


# --------------------------------------------------------------------------------- local limiter
def L(key="k", scope="identity", cap=3.0, rate=1.0):
    return Limit(scope, key, cap, rate)


def test_local_limiter_allows_a_burst_then_denies_with_a_retry_hint():
    c = Clock()
    lim = LocalLimiter(clock=c)
    assert [lim.check([L()]).allowed for _ in range(4)] == [True, True, True, False]
    r = lim.check([L()])
    assert not r.allowed and r.scope == "identity" and 0 < r.retry_after_ms <= 1000 and r.degraded


def test_local_limiter_refills_over_time():
    c = Clock()
    lim = LocalLimiter(clock=c)
    for _ in range(3):
        lim.check([L()])
    assert not lim.check([L()]).allowed
    c.t += 2.0
    assert [lim.check([L()]).allowed for _ in range(3)] == [True, True, False]  # two tokens came back, third was spent


def test_local_limiter_is_all_or_nothing_across_dimensions():
    lim = LocalLimiter(clock=Clock())
    ip, ident = L("ip", "ip", cap=100), L("id", "identity", cap=1)
    assert lim.check([ip, ident]).allowed
    r = lim.check([ip, ident])
    assert not r.allowed and r.scope == "identity"
    for _ in range(99):  # the denied call charged nothing to the roomy IP bucket
        assert lim.check([ip]).allowed
    assert not lim.check([ip]).allowed


def test_divisor_keeps_the_cluster_total_near_the_configured_limit():
    one = LocalLimiter(divisor=1, clock=Clock())
    three = LocalLimiter(divisor=3, clock=Clock())
    big = L(cap=30, rate=0.001)
    assert sum(one.check([big]).allowed for _ in range(100)) == 30
    assert sum(three.check([big]).allowed for _ in range(100)) == 10  # 3 replicas x 10 = 30 cluster-wide
    assert LocalLimiter(divisor=100, clock=Clock()).check([L(cap=3)]).allowed  # capacity never rounds down to nothing


def test_local_limiter_memory_is_bounded_under_a_key_flood():
    c = Clock()
    lim = LocalLimiter(max_keys=1000, clock=c)
    for i in range(5000):
        c.t += 0.001
        lim.check([L(f"flood-{i}")])
    assert len(lim._b) <= 1000


def test_local_limiter_forgets_idle_keys():
    c = Clock()
    lim = LocalLimiter(max_keys=10, clock=c)
    for i in range(10):
        lim.check([L(f"old-{i}")])
    c.t += 700
    for i in range(5):
        lim.check([L(f"new-{i}")])
    assert not any(k.startswith("old-") for k in lim._b) or len(lim._b) <= 10


def test_replica_count_env_sets_the_divisor(monkeypatch):
    monkeypatch.setenv("FD_REPLICA_COUNT", "4")
    assert RedisLimiter(_FakeRedis()).local.divisor == 4


class _FakeRedis:
    def register_script(self, _):
        async def call(**_k):
            raise ConnectionError

        return call


# ----------------------------------------------------------------------------- the fail-mode policy
class DeadRedis(_FakeRedis):
    pass


async def test_policy_local_open_and_closed_when_redis_is_down():
    lim = RedisLimiter(DeadRedis(), local=LocalLimiter(clock=Clock()))
    deg0 = {m: val("fd_limiter_degraded_total", mode=m) for m in ("local", "open", "closed")}
    errs0 = val("fd_redis_errors_total", op="limiter")

    local = [await lim.check_policy([L(cap=2)], "local") for _ in range(3)]
    assert [r.allowed for r in local] == [True, True, False] and all(r.degraded for r in local)

    assert [(await lim.check_policy([L(cap=1)], "open")).allowed for _ in range(10)] == [True] * 10  # no limit at all
    closed = await lim.check_policy([L()], "closed")
    assert not closed.allowed and closed.degraded and closed.scope == "limiter"

    assert val("fd_limiter_degraded_total", mode="local") == deg0["local"] + 3
    assert val("fd_limiter_degraded_total", mode="open") == deg0["open"] + 10
    assert val("fd_limiter_degraded_total", mode="closed") == deg0["closed"] + 1
    assert val("fd_redis_errors_total", op="limiter") == errs0 + 14


async def test_policy_is_not_degraded_when_redis_works(redis_client):
    lim = RedisLimiter(redis_client, "pol")
    r = await lim.check_policy([L("healthy", cap=2)], "local")
    assert r.allowed and not r.degraded
    assert metrics.LIMITER_DEGRADED is not None


async def test_check_or_open_still_means_open():
    r = await RedisLimiter(DeadRedis(), local=LocalLimiter(clock=Clock())).check_or_open([L(cap=1)])
    assert r.allowed and r.degraded
