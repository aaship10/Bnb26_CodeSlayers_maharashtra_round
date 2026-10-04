"""Signals + risk engine + decision log through the real gate, stub app, Postgres and Redis."""
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.defence import config_source, gate as gate_mod
from app.defence.decisionlog.writer import DecisionLog
from app.defence.pow import protocol
from app.defence.captcha.providers import MOCK_UI_TOKEN
from app.defence.settings import get_settings
from app.defence.signals import counts, timing

EV = "11111111-1111-1111-1111-111111111111"
EVU = uuid.UUID(EV)
BROWSER = {"User-Agent": "Mozilla/5.0 Chrome/126.0", "Accept-Language": "en-IN"}
ADM = {"X-Admin-Token": "adm"}
RISK_ONLY = {"preset": "custom", "layers": {"signals": {"enabled": True}, "risk": {"enabled": True}}}
RISK_CHALLENGES = {"preset": "custom", "layers": {"signals": {"enabled": True}, "risk": {"enabled": True},
                                                  "pow": {"enabled": True, "mode": "risk"}, "captcha": {"enabled": True, "mode": "risk"}}}


@pytest.fixture(autouse=True)
def reset(rt):
    gate_mod._providers.clear()
    counts._inflight.clear()
    yield


async def add_identity(rt, user_id, *, device=None, ip="198.51.100.5", subnet="198.51.100.0/24", verified_ago_s=3 * 86400,
                       otp_ms=30_000, flags=None, email=None):
    now = datetime.now(timezone.utc)
    async with rt.pg.connection() as conn:
        await conn.execute(
            """INSERT INTO defence.identities (user_id, email_canonical, email_original, email_domain, email_flags, display_name,
                   registration_ip, registration_subnet, device_id, registered_at, verified_at, otp_latency_ms)
               VALUES (%s,%s,%s,'example-college.edu',%s::jsonb,'T',%s::inet,%s,%s,%s,%s,%s)""",
            (user_id, email or f"{user_id}@example-college.edu", email or f"{user_id}@example-college.edu", json.dumps(flags or {}),
             ip, subnet, device, now - timedelta(seconds=verified_ago_s + 30), now - timedelta(seconds=verified_ago_s), otp_ms),
        )


async def fillers(rt, n, *, device=None, ip=None, subnet=None):
    """n other identities sharing a device / IP / subnet with the person under test."""
    now = datetime.now(timezone.utc)
    rows = []
    for _ in range(n):
        uid = uuid.uuid4()
        rows.append((uid, f"{uid}@example-college.edu", f"{uid}@example-college.edu", device, ip, subnet, now - timedelta(days=3)))
    async with rt.pg.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO defence.identities (user_id, email_canonical, email_original, email_domain, display_name, device_id,
                       registration_ip, registration_subnet, registered_at, verified_at, otp_latency_ms)
                   VALUES (%s,%s,%s,'example-college.edu','F',%s,%s::inet,%s,%s,%s,30000)""",
                [(u, e, o, d, i, sn, t, t) for (u, e, o, d, i, sn, t) in rows],
            )


async def enter(client, headers, extra=None):
    return await client.post(f"/events/{EV}/enter", headers={**headers, **(extra or {})})


def challenge_of(r):
    assert r.status_code == 403 and r.json()["code"] == "CHALLENGE_REQUIRED", r.text
    return r.json()["details"]["challenge"]


async def entry(rt, uid):
    async with rt.pg.connection() as conn:
        return await (await conn.execute("SELECT weight, risk FROM entries WHERE user_id = %s", (uid,))).fetchone()


async def decisions(client, **params):
    q = "&".join(f"{k}={v}" for k, v in {"event_id": EV, **params}.items())
    r = await client.get(f"/admin/defence/decisions?{q}", headers=ADM)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------------------- weights
async def test_a_clean_person_gets_full_weight_and_a_breakdown(stub, client, rt):
    await stub.set_defences(RISK_ONLY)
    uid, h = await stub.user()
    await add_identity(rt, uid, device=str(uuid.uuid4()))
    r = await enter(client, {**h, **BROWSER})
    assert r.status_code == 201
    row = await entry(rt, uid)
    assert float(row["weight"]) == 1.0 and row["risk"]["score"] == 0.0 and row["risk"]["band"] == "low"


async def test_device_farm_member_is_down_weighted_to_half(stub, client, rt):
    await stub.set_defences(RISK_ONLY)
    uid, h = await stub.user()
    dev = str(uuid.uuid4())
    await add_identity(rt, uid, device=dev, otp_ms=1000)  # instant OTP
    await fillers(rt, 11, device=dev)  # 12 accounts on one device
    r = await enter(client, h)  # plain httpx user agent => header anomaly too
    assert r.status_code == 201  # down-weighted, NOT rejected
    row = await entry(rt, uid)
    assert float(row["weight"]) == 0.5 and row["risk"]["band"] == "half"
    names = {s["name"] for s in row["risk"]["signals"]}
    assert {"accounts_per_device", "otp_latency", "header_anomaly"} <= names  # explainable


async def test_the_worst_case_lands_on_a_quarter_never_a_reject(stub, client, rt):
    await stub.set_defences(RISK_ONLY)
    uid, h = await stub.user()
    dev = str(uuid.uuid4())
    await add_identity(rt, uid, device=dev, otp_ms=500, verified_ago_s=60, flags={"sequential_pattern": True, "pattern_cluster_size": 40})
    await fillers(rt, 30, device=dev)
    r = await enter(client, h)
    assert r.status_code == 201
    row = await entry(rt, uid)
    assert float(row["weight"]) == 0.25 and row["risk"]["score"] >= 0.75


async def test_a_shared_campus_ip_alone_is_never_penalised(stub, client, rt):
    """1,200 accounts behind one NAT address, and a person among them with an otherwise clean profile."""
    await stub.set_defences(RISK_CHALLENGES)
    uid, h = await stub.user()
    await add_identity(rt, uid, device=str(uuid.uuid4()), ip="10.20.0.9", subnet="10.20.0.0/24")
    await fillers(rt, 1200, ip="10.20.0.9", subnet="10.20.0.0/24")
    r = await enter(client, {**h, **BROWSER})
    assert r.status_code == 201, r.text  # no challenge at all
    row = await entry(rt, uid)
    assert float(row["weight"]) == 1.0 and row["risk"]["band"] == "low"
    net = {s["name"] for s in row["risk"]["signals"]}
    assert {"accounts_per_ip", "accounts_per_subnet", "registration_velocity"} <= net  # the evidence IS seen...
    assert row["risk"]["score"] <= 0.15 + 1e-6  # ...but capped: it cannot challenge anyone alone


async def test_a_person_with_no_identity_record_is_not_penalised(stub, client, rt):
    await stub.set_defences(RISK_ONLY)
    uid, h = await stub.user()  # created directly: no defence.identities row
    r = await enter(client, {**h, **BROWSER})
    assert r.status_code == 201
    row = await entry(rt, uid)
    assert float(row["weight"]) == 1.0 and row["risk"]["score"] == 0.0


# --------------------------------------------------------- risk-driven challenges and difficulty
async def test_risk_mode_challenges_only_risky_people(stub, client, rt):
    await stub.set_defences(RISK_CHALLENGES)
    clean, hc = await stub.user()
    await add_identity(rt, clean, device=str(uuid.uuid4()))
    assert (await enter(client, {**hc, **BROWSER})).status_code == 201  # nothing asked of an ordinary person

    risky, hr = await stub.user()
    dev = str(uuid.uuid4())
    await add_identity(rt, risky, device=dev, otp_ms=1000)
    await fillers(rt, 11, device=dev)
    ch = challenge_of(await enter(client, {**hr, **BROWSER}))
    assert ch["type"] == "pow"


async def test_difficulty_rises_with_risk_within_the_cap(stub, client, rt):
    await stub.set_defences(RISK_CHALLENGES)
    bits = {}
    for label, n_dev, otp, age in (("medium", 11, 1000, 3 * 86400), ("high", 30, 500, 60)):
        uid, h = await stub.user()
        dev = str(uuid.uuid4())
        await add_identity(rt, uid, device=dev, otp_ms=otp, verified_ago_s=age, flags={"sequential_pattern": label == "high"})
        await fillers(rt, n_dev, device=dev)
        bits[label] = challenge_of(await enter(client, {**h, **BROWSER}))["pow"]["difficulty_bits"]
    assert 14 < bits["medium"] < bits["high"] <= 20  # base 14 + up to risk_bits_max 6


async def test_risky_person_solves_pow_then_captcha_and_is_down_weighted(stub, client, rt):
    await stub.set_defences(RISK_CHALLENGES)
    uid, h = await stub.user()
    dev = str(uuid.uuid4())
    await add_identity(rt, uid, device=dev, otp_ms=1000)
    await fillers(rt, 11, device=dev)
    hb = {**h, **BROWSER}
    pow_ch = challenge_of(await enter(client, hb))
    sol = (pow_ch["id"], protocol.solve(pow_ch["pow"]["prefix"], pow_ch["pow"]["difficulty_bits"]))
    cap = challenge_of(await enter(client, hb, {"X-Challenge-Id": sol[0], "X-Challenge-Solution": sol[1]}))
    assert cap["type"] == "captcha"
    r = await enter(client, hb, {"X-Challenge-Id": cap["id"], "X-Challenge-Solution": MOCK_UI_TOKEN})
    assert r.status_code == 201
    row = await entry(rt, uid)
    assert float(row["weight"]) in (1.0, 0.5, 0.25) and row["risk"]["challenges_passed"] == ["pow", "captcha"]
    assert row["risk"]["score"] >= 0.3


# -------------------------------------------------------------------------- hot reload
async def test_toggling_the_risk_layer_takes_effect_without_a_restart(stub, client, rt, monkeypatch):
    monkeypatch.setattr(config_source, "CONFIG_TTL_S", 0.2)
    uid, h = await stub.user()
    dev = str(uuid.uuid4())
    await add_identity(rt, uid, device=dev, otp_ms=1000)
    await fillers(rt, 11, device=dev)
    uid2, h2 = await stub.user()
    await add_identity(rt, uid2, device=dev, otp_ms=1000)

    assert (await enter(client, h)).status_code == 201  # layers off: weight 1.0, no risk
    assert float((await entry(rt, uid))["weight"]) == 1.0 and (await entry(rt, uid))["risk"] is None

    await stub.set_defences(RISK_ONLY)
    await asyncio.sleep(0.3)
    assert (await enter(client, h2)).status_code == 201
    assert float((await entry(rt, uid2))["weight"]) < 1.0  # same kind of person, now down-weighted


# -------------------------------------------------------- Postgres is the truth, Redis a cache
async def test_cluster_counts_rebuild_from_postgres_after_a_redis_flush(stub, rt, redis_client):
    dev = str(uuid.uuid4())
    await fillers(rt, 7, device=dev, ip="198.51.100.77", subnet="198.51.100.0/24")
    env = get_settings().env
    first = [await counts.device_accounts(rt, env, dev), await counts.ip_accounts(rt, env, "198.51.100.77"),
             await counts.subnet_accounts(rt, env, "198.51.100.0/24")]
    assert first == [7, 7, 7]
    assert await redis_client.get(f"fd:{env}:sig:dev:{dev}") == "7"  # cached
    await redis_client.flushdb()  # Redis lost everything
    assert await redis_client.get(f"fd:{env}:sig:dev:{dev}") is None
    again = [await counts.device_accounts(rt, env, dev), await counts.ip_accounts(rt, env, "198.51.100.77"),
             await counts.subnet_accounts(rt, env, "198.51.100.0/24")]
    assert again == first  # recomputed from the durable table
    # the cache is just a cache: it can be stale for <= 60 s, and is refreshed from Postgres on expiry
    await fillers(rt, 3, device=dev)
    assert await counts.device_accounts(rt, env, dev) == 7
    await redis_client.delete(f"fd:{env}:sig:dev:{dev}")
    assert await counts.device_accounts(rt, env, dev) == 10


async def test_a_thundering_herd_causes_one_count_query(stub, rt, monkeypatch):
    dev = str(uuid.uuid4())
    await fillers(rt, 4, device=dev)
    calls = 0
    real = counts._count

    async def slow(*a, **k):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return await real(*a, **k)

    monkeypatch.setattr(counts, "_count", slow)
    got = await asyncio.gather(*[counts.device_accounts(rt, "herd", dev) for _ in range(60)])
    assert set(got) == {4} and calls == 1  # 60 concurrent requests, one query


async def test_burst_signal_counts_the_same_subnet_in_the_same_ten_minutes(stub, rt):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    base = now - timedelta(seconds=(now.timestamp() % 600))  # start of the current 10-minute bucket
    me = uuid.uuid4()
    async with rt.pg.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO defence.identities (user_id, email_canonical, email_original, email_domain, display_name,
                       registration_subnet, registered_at, verified_at, otp_latency_ms)
                   VALUES (%s,%s,%s,'x.edu','B','203.0.113.0/24',%s,%s,1000)""",
                [(u, f"{u}@x.edu", f"{u}@x.edu", base + timedelta(seconds=10), base + timedelta(seconds=10)) for u in [me] + [uuid.uuid4() for _ in range(49)]]
                + [(u, f"{u}@x.edu", f"{u}@x.edu", base - timedelta(hours=2), base - timedelta(hours=2)) for u in [uuid.uuid4() for _ in range(5)]],
            )
    n = await counts.subnet_burst(rt, "burst", "203.0.113.0/24", base + timedelta(seconds=10))
    assert n == 49  # the other 49 in the bucket; the 5 from two hours earlier and the person themself excluded


# ---------------------------------------------------------------------------- timing signal
async def test_a_machine_regular_enter_loop_raises_the_timing_signal(stub, client, rt):
    await stub.set_defences(RISK_ONLY)
    uid, h = await stub.user()
    await add_identity(rt, uid, device=str(uuid.uuid4()))
    env = get_settings().env
    # 18 requests exactly 1.000 s apart, the last one 1 s ago: the live /enter below continues the loop
    # (it is itself recorded, so a handful of samples would be dominated by that one extra gap).
    t_last = int(time.time() * 1000) - 1000
    for i in range(18):
        await timing.record(rt.redis, env, EV, str(uid), now_ms=t_last - (17 - i) * 1000)
    assert (await enter(client, {**h, **BROWSER})).status_code == 201
    risk = (await entry(rt, uid))["risk"]
    t = next(s for s in risk["signals"] if s["name"] == "timing_regularity")
    assert t["value"] > 0.6 and t["contribution"] > 0.2 and t["detail"]["samples"] >= 17


async def test_timing_is_recorded_by_the_real_requests_and_status_polling_is_not_counted(stub, client, rt):
    await stub.set_defences(RISK_ONLY)
    uid, h = await stub.user()
    for _ in range(6):
        await client.get(f"/events/{EV}/status", headers=h)  # the frontend's periodic polling
    assert await timing.intervals_ms(rt.redis, get_settings().env, EV, str(uid)) == []
    await client.post("/defence/challenge", json={"event_id": EV}, headers=h)  # a bot-style endpoint
    await client.post("/defence/challenge", json={"event_id": EV}, headers=h)
    assert len(await timing.intervals_ms(rt.redis, get_settings().env, EV, str(uid))) == 1


# ---------------------------------------------------------------------------- failure modes
class DeadRedis:
    def register_script(self, _):
        async def boom(**_k):
            raise ConnectionError("down")

        return boom

    def pipeline(self, **_k):
        raise ConnectionError("down")

    async def get(self, *a, **k):
        raise ConnectionError("down")

    async def set(self, *a, **k):
        raise ConnectionError("down")

    async def lrange(self, *a, **k):
        raise ConnectionError("down")


async def test_with_redis_down_signals_still_work_from_postgres(stub, client, rt):
    from app.defence.ratelimit.bucket import RedisLimiter

    await stub.set_defences(RISK_ONLY)
    uid, h = await stub.user()
    dev = str(uuid.uuid4())
    await add_identity(rt, uid, device=dev, otp_ms=1000)
    await fillers(rt, 11, device=dev)
    rt.redis, rt.limiter = DeadRedis(), RedisLimiter(DeadRedis())
    assert (await enter(client, h)).status_code == 201
    assert float((await entry(rt, uid))["weight"]) < 1.0  # device count came from Postgres, not the cache


async def test_a_signal_outage_does_not_block_entry_but_is_not_silent(stub, client, rt, monkeypatch, caplog):
    await stub.set_defences(RISK_ONLY)
    uid, h = await stub.user()

    async def boom(*a, **k):
        raise RuntimeError("signal backend down")

    monkeypatch.setattr(gate_mod, "collect", boom)
    with caplog.at_level("ERROR", logger="fd.gate"):
        r = await enter(client, {**h, **BROWSER})
    assert r.status_code == 201 and float((await entry(rt, uid))["weight"]) == 1.0
    assert "risk assessment failed" in caplog.text


# --------------------------------------------------------------------------- decision log
async def test_every_decision_is_logged_including_challenge_and_reject(stub, client, rt):
    await stub.set_defences({"preset": "custom", "layers": {"pow": {"enabled": True, "mode": "always"},
                                                            "signals": {"enabled": True}, "risk": {"enabled": True}}})
    uid, h = await stub.user()
    dev = str(uuid.uuid4())
    await add_identity(rt, uid, device=dev, otp_ms=1000)
    await fillers(rt, 11, device=dev)
    hb = {**h, **BROWSER, "X-Device-Id": dev, "X-Forwarded-For": "203.0.113.50"}
    ch = challenge_of(await enter(client, hb))
    forged = await enter(client, hb, {"X-Challenge-Id": ch["id"][:-1] + ("0" if ch["id"][-1] != "0" else "1"), "X-Challenge-Solution": "1"})
    assert forged.json()["code"] == "REJECTED"
    ok = await enter(client, hb, {"X-Challenge-Id": ch["id"], "X-Challenge-Solution": protocol.solve(ch["pow"]["prefix"], ch["pow"]["difficulty_bits"])})
    assert ok.status_code == 201

    got = (await decisions(client))["decisions"]
    assert [d["action"] for d in got] == ["CHALLENGE", "REJECT", "ALLOW"]
    chal, rej, allow = got
    assert chal["layer"] == "pow" and chal["weight"] is None and chal["score"] > 0.3 and chal["signals"]["signals"]
    assert rej["layer"] == "pow" and rej["weight"] is None
    assert allow["weight"] in (1.0, 0.5, 0.25) and allow["signals"]["challenges_passed"] == ["pow"]
    assert chal["ip"] == "203.0.113.50" and chal["device"] == dev and chal["user_id"] == str(uid)
    assert all(d["event_id"] == EV and d["ts"].endswith("Z") for d in got)
    assert not any("sim_label" in json.dumps(d) for d in got)  # ground truth never appears here


async def test_decision_log_without_the_risk_engine_still_logs(stub, client):
    await stub.set_defences({"preset": "rate_limit+pow"})
    _, h = await stub.user()
    await enter(client, h)
    d = (await decisions(client))["decisions"][0]
    assert d["action"] == "CHALLENGE" and d["score"] is None


async def test_decisions_endpoint_pagination_filter_and_auth(stub, client):
    await stub.set_defences({"preset": "rate_limit+pow"})
    for _ in range(5):
        _, h = await stub.user()
        await enter(client, h)
    assert (await client.get(f"/admin/defence/decisions?event_id={EV}")).status_code == 401  # admin only
    page1 = await decisions(client, limit=2)
    assert len(page1["decisions"]) == 2 and page1["next_cursor"] is not None
    page2 = await decisions(client, limit=2, cursor=page1["next_cursor"])
    ids = [d["id"] for d in page1["decisions"] + page2["decisions"]]
    assert ids == sorted(ids) and len(set(ids)) == 4  # keyset pagination: no gaps, no repeats
    everything = await decisions(client, limit=100)
    assert len(everything["decisions"]) == 5 and everything["next_cursor"] is None
    assert (await decisions(client, action="ALLOW"))["decisions"] == []
    assert (await client.get(f"/admin/defence/decisions?event_id={EV}&limit=0", headers=ADM)).status_code == 422
    assert (await client.get(f"/admin/defence/decisions?event_id={EV}&action=BOGUS", headers=ADM)).status_code == 422


async def test_decisions_summary(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow"})
    for _ in range(3):
        _, h = await stub.user()
        await enter(client, h)
    r = await client.get(f"/admin/defence/decisions/summary?event_id={EV}", headers=ADM)
    body = r.json()
    assert body["counts"] == [{"action": "CHALLENGE", "weight": None, "layer": "pow", "n": 3}]
    assert body["log"]["dropped"] == 0 and body["log"]["failed_batches"] == 0


async def test_decisions_are_scoped_per_event(stub, client):
    await stub.set_defences({"preset": "rate_limit+pow"})
    _, h = await stub.user()
    await enter(client, h)
    assert (await decisions(client))["decisions"]
    other = await client.get(f"/admin/defence/decisions?event_id={uuid.uuid4()}", headers=ADM)
    assert other.json()["decisions"] == []


# ------------------------------------------------- the writer on its own (never blocks, never throws)
def row(i=0):
    return {"event_id": EVU, "user_id": uuid.uuid4(), "ts": datetime.now(timezone.utc), "action": "ALLOW", "weight": 1.0}


async def test_writer_drops_instead_of_blocking_when_the_queue_is_full(rt):
    log = DecisionLog(rt.pg, max_queue=3)  # not started: nothing drains it
    results = [log.record(row()) for _ in range(10)]
    assert results == [True] * 3 + [False] * 7 and log.dropped == 7
    await log.flush()
    assert log.written == 3


async def test_writer_survives_a_failing_database_and_counts_it(rt):
    class BrokenPool:
        def connection(self):
            raise ConnectionError("database down")

    log = DecisionLog(BrokenPool())
    log.record(row())
    await log.flush()
    assert log.failed_batches == 1 and log.written == 0  # dropped (analytics), no exception escaped


async def test_writer_flushes_on_stop_and_batches(rt):
    log = DecisionLog(rt.pg, batch=10, interval_s=60)
    log.start()
    for i in range(25):
        log.record(row(i))
    await log.stop()  # clean shutdown writes what is queued
    assert log.written == 25
    async with rt.pg.connection() as conn:
        n = await (await conn.execute("SELECT count(*) AS n FROM defence.decisions WHERE event_id = %s", (EVU,))).fetchone()
    assert n["n"] >= 25


async def test_record_is_synchronous_and_cheap(rt):
    log = DecisionLog(rt.pg, max_queue=100_000)
    start = time.perf_counter()
    for i in range(5000):
        log.record(row(i))
    assert (time.perf_counter() - start) / 5000 < 0.0005  # well under half a millisecond per decision
