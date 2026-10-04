import asyncio
import random

import pytest
from app.defence.errors import ErrorCode
from app.defence.ratelimit.bucket import Limit, LimitResult, RedisLimiter, hash_id
from app.defence.ratelimit.errors import rate_limited


@pytest.fixture
async def limiter(redis_client):
    return RedisLimiter(redis_client, "t")


def L(key="k", scope="identity", cap=3, rate=1.0):
    return Limit(scope, key, cap, rate)


async def test_burst_then_denied_with_retry_hint(limiter):
    for _ in range(3):
        assert (await limiter.check([L()])).allowed
    r = await limiter.check([L()])
    assert not r.allowed and r.scope == "identity"
    assert 0 < r.retry_after_ms <= 1000  # one token at 1/s


async def test_tokens_refill_over_time(limiter):
    fast = L(cap=2, rate=20.0)  # one token per 50 ms
    assert (await limiter.check([fast])).allowed and (await limiter.check([fast])).allowed
    assert not (await limiter.check([fast])).allowed
    await asyncio.sleep(0.12)
    assert (await limiter.check([fast])).allowed


async def test_all_or_nothing_across_dimensions(limiter):
    ip, ident = L("ip", "ip", cap=100), L("id", "identity", cap=1)
    assert (await limiter.check([ip, ident])).allowed  # uses the only identity token
    r = await limiter.check([ip, ident])
    assert not r.allowed and r.scope == "identity"
    # The rejected call must not have charged the roomy ip bucket.
    # 100 capacity: 1 charged by the first call, so 99 more single-bucket calls must pass.
    for _ in range(99):
        assert (await limiter.check([ip])).allowed
    assert not (await limiter.check([ip])).allowed


async def test_reports_first_denied_scope_and_longest_wait(limiter):
    slow = Limit("device", "dev", 1, 0.1)  # 10 s per token
    fast = Limit("ip", "ip", 1, 10.0)
    await limiter.check([slow, fast])
    r = await limiter.check([fast, slow])
    assert not r.allowed and r.scope in ("ip", "device")
    assert r.retry_after_ms >= 9000  # the slowest denied bucket decides


async def test_buckets_are_independent_per_key(limiter):
    assert (await limiter.check([L("a", cap=1)])).allowed
    assert (await limiter.check([L("b", cap=1)])).allowed
    assert not (await limiter.check([L("a", cap=1)])).allowed


async def test_atomic_under_concurrency(limiter):
    # 200 concurrent requests against capacity 25 (no refill during the test): exactly 25 pass.
    lim = L("race", cap=25, rate=0.0001)
    results = await asyncio.gather(*[limiter.check([lim]) for _ in range(200)])
    assert sum(r.allowed for r in results) == 25


async def test_keys_get_a_ttl_so_idle_identities_cost_nothing(limiter, redis_client):
    await limiter.check([L("ttl", cap=2, rate=1.0)])
    ttl = await redis_client.pttl("fd:t:rl:ttl")
    assert 0 < ttl <= 3000  # capacity/rate = 2 s, plus 1 s slack


async def test_denied_requests_write_nothing(limiter, redis_client):
    lim = L("quiet", cap=1, rate=0.001)
    await limiter.check([lim])
    before = await redis_client.hgetall("fd:t:rl:quiet")
    for _ in range(5):
        assert not (await limiter.check([lim])).allowed
    assert await redis_client.hgetall("fd:t:rl:quiet") == before


async def test_cost_larger_than_capacity_is_a_programming_error(limiter):
    with pytest.raises(ValueError):
        await limiter.check([L(cap=1)], cost=2)


async def test_empty_limits_allow(limiter):
    assert (await limiter.check([])).allowed


async def test_redis_failure_fails_open_and_is_flagged():
    class Dead:
        def register_script(self, _):
            async def boom(**_kw):
                raise ConnectionError("redis is down")

            return boom

    r = await RedisLimiter(Dead()).check_or_open([L()])
    assert r.allowed and r.degraded


def test_hash_id_hides_the_address():
    h = hash_id("rahul@example-college.edu")
    assert "rahul" not in h and len(h) == 24 and h == hash_id("rahul@example-college.edu")


# ---- 429 contract
def test_429_contract_shape_and_jitter_bounds():
    res = LimitResult(False, "ip", 2000)
    seen = set()
    for seed in range(50):
        e = rate_limited(res, 0.25, random.Random(seed))
        assert e.status == 429 and e.code is ErrorCode.RATE_LIMITED
        ms = e.details["retry_after_ms"]
        assert 2000 <= ms <= 2500  # stretched by 0..25 %, never shortened
        assert e.details["scope"] == "ip"
        assert e.headers["Retry-After"] == str(-(-ms // 1000))
        seen.add(ms)
    assert len(seen) > 10  # actually jittered: clients do not retry in lockstep


def test_429_header_is_whole_seconds_and_at_least_one():
    e = rate_limited(LimitResult(False, "device", 5), 0.0)
    assert e.headers["Retry-After"] == "1" and e.details["retry_after_ms"] == 5


def test_cannot_build_429_from_allowed_result():
    with pytest.raises(ValueError):
        rate_limited(LimitResult(True))
