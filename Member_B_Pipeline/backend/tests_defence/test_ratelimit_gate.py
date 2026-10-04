"""Per-endpoint rate limiting through the real stub app, Postgres and Redis (or fakeredis)."""
import asyncio
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from app.defence import config_source
from app.defence.config import validate_defences
from app.defence.identity import tokens
from app.defence.ratelimit.bucket import RedisLimiter

EV = "11111111-1111-1111-1111-111111111111"


def custom(**limits):
    """A custom config with only the given status/enter limits, e.g. custom(status={"ip": (5, 0.001)})."""
    lim = {ep: {dim: {"capacity": c, "refill_per_s": r} for dim, (c, r) in dims.items()} for ep, dims in limits.items()}
    return {"preset": "custom", "layers": {"rate_limit": {"enabled": True, "limits": lim}}}


async def hammer(client, n, method="GET", path=f"/events/{EV}/status", headers=None):
    out = []
    for _ in range(n):
        r = await client.request(method, path, headers=headers)
        out.append(r)
    return out


# ------------------------------------------------------------- efficacy: layer off vs on
async def test_without_the_layer_a_flood_is_never_limited(stub, client):
    _, h = await stub.user()
    rs = await hammer(client, 25, headers=h)
    assert {r.status_code for r in rs} == {200}


async def test_with_the_layer_the_same_flood_is_limited(stub, client):
    await stub.set_defences({"preset": "rate_limit"})
    _, h = await stub.user()
    rs = await hammer(client, 25, headers=h)
    codes = [r.status_code for r in rs]
    assert codes.count(200) == 2 and codes.count(429) == 23  # status identity bucket: burst of 2
    first = next(r for r in rs if r.status_code == 429)
    b = first.json()
    assert b["code"] == "RATE_LIMITED" and b["details"]["scope"] == "identity" and b["details"]["retry_after_ms"] > 0
    assert int(first.headers["retry-after"]) >= 1


async def test_enter_flood_is_limited_and_entry_stays_unique(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit"})
    uid, h = await stub.user()
    rs = await hammer(client, 20, "POST", f"/events/{EV}/enter", h)
    codes = [r.status_code for r in rs]
    assert codes.count(429) >= 15 and codes[0] == 201
    async with rt.pg.connection() as conn:
        n = await (await conn.execute("SELECT count(*) AS n FROM entries WHERE user_id = %s", (uid,))).fetchone()
    assert n["n"] == 1  # limiting never creates or duplicates entries


# ------------------------------------------------------------------------- dimensions
async def test_identities_have_separate_buckets(stub, client):
    await stub.set_defences({"preset": "rate_limit"})
    (_, a), (_, b) = await stub.user(), await stub.user()
    ra = await hammer(client, 5, headers=a)
    rb = await hammer(client, 2, headers=b)
    assert any(r.status_code == 429 for r in ra)
    assert [r.status_code for r in rb] == [200, 200]  # a's flood did not touch b (same IP, generous ip bucket)


async def test_unauthenticated_flood_is_shed_by_ip_before_auth(stub, client):
    await stub.set_defences(custom(status={"ip": (5, 0.001)}))
    rs = await hammer(client, 9)  # no token at all
    codes = [r.status_code for r in rs]
    assert codes[:5] == [401] * 5 and codes[5:] == [429] * 4
    assert rs[-1].json()["details"]["scope"] == "ip"


async def test_device_bucket_applies_even_when_identity_rotates(stub, client):
    await stub.set_defences(custom(status={"device": (3, 0.001)}))
    dev = {"X-Device-Id": str(uuid.uuid4())}
    codes = []
    for _ in range(6):
        _, h = await stub.user()  # a new identity each time: the Sybil pattern
        codes.append((await client.get(f"/events/{EV}/status", headers={**h, **dev})).status_code)
    assert codes == [200, 200, 200, 429, 429, 429]


async def test_garbage_device_id_gives_no_free_bucket(stub, client):
    await stub.set_defences(custom(status={"device": (1, 0.001)}))
    _, h = await stub.user()
    codes = [(await client.get(f"/events/{EV}/status", headers={**h, "X-Device-Id": "garbage"})).status_code for _ in range(4)]
    assert codes == [200] * 4  # an unparseable id is ignored, not trusted as a key


# ----------------------------------------------------------------- spoofing / client IP
async def _client_from(peer, app_client):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 40000)), base_url="http://t")


async def test_rotating_forwarding_headers_from_an_untrusted_peer_earn_no_budget(stub, rt):
    await stub.set_defences(custom(status={"ip": (4, 0.001)}))
    async with await _client_from("198.51.100.9", None) as c:  # not in TRUSTED_PROXIES
        codes = []
        for i in range(8):
            r = await c.get(f"/events/{EV}/status", headers={
                "X-Forwarded-For": f"203.0.113.{i}", "X-Real-IP": f"203.0.113.{100 + i}", "X-Sim-Client-IP": f"203.0.113.{200 + i}"})
            codes.append(r.status_code)
    assert codes == [401] * 4 + [429] * 4


async def test_trusted_proxy_forwarded_ip_is_what_gets_limited(stub, client):
    await stub.set_defences(custom(status={"ip": (2, 0.001)}))
    a = [(await client.get(f"/events/{EV}/status", headers={"X-Forwarded-For": "203.0.113.1"})).status_code for _ in range(4)]
    b = [(await client.get(f"/events/{EV}/status", headers={"X-Forwarded-For": "203.0.113.2"})).status_code for _ in range(2)]
    assert a == [401, 401, 429, 429] and b == [401, 401]  # per real client, not per proxy


async def test_subnet_bucket_aggregates_addresses_in_a_24(stub, client):
    await stub.set_defences(custom(status={"subnet": (3, 0.001)}))
    codes = [(await client.get(f"/events/{EV}/status", headers={"X-Forwarded-For": f"203.0.113.{i}"})).status_code for i in range(1, 6)]
    assert codes == [401, 401, 401, 429, 429]
    other = await client.get(f"/events/{EV}/status", headers={"X-Forwarded-For": "198.51.100.1"})
    assert other.status_code == 401


# ------------------------------------------------------------------- config caching / toggling
async def test_toggle_takes_effect_within_the_cache_ttl(stub, client, monkeypatch):
    monkeypatch.setattr(config_source, "CONFIG_TTL_S", 0.3)
    _, h = await stub.user()
    assert all(r.status_code == 200 for r in await hammer(client, 5, headers=h))
    # flip the layer on directly in the database, WITHOUT clearing the cache
    cfg = validate_defences({"preset": "rate_limit"}).model_dump(mode="json")
    async with stub_conn() as conn:
        await conn.execute("UPDATE events SET config = %s::jsonb WHERE id = %s", (json.dumps({"defences": cfg}), EV))
    await asyncio.sleep(0.4)
    codes = [r.status_code for r in await hammer(client, 6, headers=h)]
    assert 429 in codes


def stub_conn():
    from app import db

    return db.get_pool().connection()


async def test_config_lookup_is_single_flight_under_concurrency(stub, client, monkeypatch):
    calls = 0
    real = config_source._load

    async def counting(event_id):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return await real(event_id)

    monkeypatch.setattr(config_source, "_load", counting)
    config_source.clear_cache()
    _, h = await stub.user()
    await asyncio.gather(*[client.get(f"/events/{EV}/status", headers=h) for _ in range(40)])
    assert calls == 1  # 40 concurrent requests, one database lookup


async def test_invalid_stored_config_keeps_last_good_value_not_none(stub, client, monkeypatch):
    monkeypatch.setattr(config_source, "CONFIG_TTL_S", 0.0)
    await stub.set_defences({"preset": "rate_limit"})
    _, h = await stub.user()
    await client.get(f"/events/{EV}/status", headers=h)  # primes the cache with a good value
    async with stub_conn() as conn:
        await conn.execute("UPDATE events SET config = '{\"defences\": {\"preset\": \"bogus\"}}'::jsonb WHERE id = %s", (EV,))
    codes = [r.status_code for r in await hammer(client, 6, headers=h)]
    assert 429 in codes  # still limited: a corrupted config must not silently disable defences


async def test_unknown_event_is_not_rate_limited_and_404s(stub, client):
    r = await client.get(f"/events/{uuid.uuid4()}/status")
    assert r.status_code in (401, 404)  # no auth => 401 first; never a 500


# ------------------------------------------------------------------------ failure policy
class DeadRedis:
    def register_script(self, _):
        async def boom(**_kw):
            raise ConnectionError("redis down")

        return boom


async def test_disabled_layer_makes_zero_redis_calls(stub, client, rt):
    rt.limiter = RedisLimiter(DeadRedis())  # would fail open and flag if it were ever called
    called = False
    orig = rt.limiter.check_or_open

    async def spy(*a, **k):
        nonlocal called
        called = True
        return await orig(*a, **k)

    rt.limiter.check_or_open = spy
    _, h = await stub.user()
    assert (await client.get(f"/events/{EV}/status", headers=h)).status_code == 200
    assert called is False


async def test_redis_down_default_is_the_local_limiter_protection_degrades_but_does_not_vanish(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit"})  # fail_mode defaults to "local"
    rt.limiter = RedisLimiter(DeadRedis())
    _, h = await stub.user()
    codes = [r.status_code for r in await hammer(client, 8, headers=h)]
    assert codes[:2] == [200, 200] and set(codes[2:]) == {429}  # same status bucket (burst 2), enforced in-process
    assert (await hammer(client, 1, headers=h))[0].json()["details"]["scope"] == "identity"


async def test_redis_down_with_fail_mode_open_lets_everything_through(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit", "fail_mode": "open"})
    rt.limiter = RedisLimiter(DeadRedis())
    _, h = await stub.user()
    assert all(r.status_code == 200 for r in await hammer(client, 8, headers=h))


async def test_redis_down_with_fail_mode_closed_returns_503(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit", "fail_mode": "closed"})
    rt.limiter = RedisLimiter(DeadRedis())
    _, h = await stub.user()
    r = await client.get(f"/events/{EV}/status", headers=h)
    assert r.status_code == 503 and r.headers["retry-after"] == "2"


async def test_garbage_event_id_is_a_validation_error_not_a_crash(stub, client):
    await stub.set_defences({"preset": "rate_limit"})
    _, h = await stub.user()
    assert (await client.get("/events/not-a-uuid/status", headers=h)).status_code == 422
