"""Registration + OTP + JWT flow against real Postgres and Redis (or fakeredis)."""
import asyncio
import time
import uuid

import jwt as pyjwt
import pytest
from app.defence.identity import limits, tokens

DOMAIN = "example-college.edu"


async def register(client, email="ann@example-college.edu", name="Ann", device=None, headers=None, **extra):
    h = dict(headers or {})
    if device:
        h["X-Device-Id"] = device
    return await client.post("/auth/register", json={"email": email, "display_name": name, "hp": "", **extra}, headers=h)


async def login(client, rt, email="ann@example-college.edu", **kw):
    r = await register(client, email, **kw)
    assert r.status_code == 202, r.text
    await asyncio.sleep(0.05)  # mail is sent from a background task
    otp = rt.mailer.last_otp()
    v = await client.post("/auth/verify", json={"email": email, "otp": otp})
    assert v.status_code == 200, v.text
    return v.json()


# ----------------------------------------------------------------- happy path
async def test_register_verify_me(client, rt):
    r = await register(client)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "otp_sent" and body["expires_in_s"] == 600 and body["server_now"].endswith("Z")
    assert "otp" not in body  # never echoed outside simulation mode
    await asyncio.sleep(0.05)
    assert rt.mailer.sent[-1].to == "ann@example-college.edu"

    v = await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": rt.mailer.last_otp()})
    assert v.status_code == 200
    j = v.json()
    assert set(j) >= {"token", "expires_at", "user_id"} and j["expires_at"].endswith("Z")

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {j['token']}"})
    assert me.status_code == 200
    assert me.json()["user_id"] == j["user_id"] and me.json()["email"] == "ann@example-college.edu"
    assert me.json()["display_name"] == "Ann" and me.json()["auth"] == "jwt"


async def test_user_row_and_identity_row_are_created_once(rt, client):
    j = await login(client, rt)
    async with rt.pg.connection() as conn:
        u = await (await conn.execute("SELECT * FROM public.users WHERE email = 'ann@example-college.edu'")).fetchone()
        i = await (await conn.execute("SELECT * FROM defence.identities WHERE email_canonical = 'ann@example-college.edu'")).fetchone()
        pend = await (await conn.execute("SELECT count(*) AS n FROM defence.pending_registrations")).fetchone()
    assert str(u["id"]) == j["user_id"] == str(i["user_id"])
    assert i["otp_latency_ms"] >= 0 and i["verified_at"] >= i["registered_at"]
    assert pend["n"] == 0  # consumed


async def test_otp_is_stored_hashed(rt, client):
    await register(client)
    await asyncio.sleep(0.05)
    otp = rt.mailer.last_otp()
    async with rt.pg.connection() as conn:
        row = await (await conn.execute("SELECT otp_hash FROM defence.pending_registrations")).fetchone()
    assert otp not in row["otp_hash"] and len(row["otp_hash"]) == 64


async def test_registration_data_is_recorded_for_signals(rt, client):
    dev = str(uuid.uuid4())
    await login(client, rt, device=dev, headers={"User-Agent": "UnitTest/1.0", "X-Forwarded-For": "203.0.113.9"})
    async with rt.pg.connection() as conn:
        i = await (await conn.execute("SELECT * FROM defence.identities")).fetchone()
    assert i["device_id"] == dev and i["user_agent_hash"] and len(i["user_agent_hash"]) == 16
    # the test client's peer is 127.0.0.1 which is a trusted proxy, so XFF applies
    assert str(i["registration_ip"]) == "203.0.113.9" and i["registration_subnet"] == "203.0.113.0/24"


async def test_returning_user_logs_in_without_creating_anything(rt, client):
    first = await login(client, rt)
    second = await login(client, rt, name="Different Name")
    assert first["user_id"] == second["user_id"]
    async with rt.pg.connection() as conn:
        n = await (await conn.execute("SELECT count(*) AS n FROM public.users")).fetchone()
        i = await (await conn.execute("SELECT display_name FROM defence.identities")).fetchone()
    assert n["n"] == 1 and i["display_name"] == "Ann"  # name from the first registration is kept


# ------------------------------------------------------------ alias handling
async def test_aliases_cannot_create_duplicate_identities(rt, client):
    ids = set()
    for email in ["j.o.e@gmail.com", "joe@gmail.com", "J.OE+drop@googlemail.com", "joe+x@gmail.com"]:
        await rt.redis.flushdb()  # this test is about identity, not about the per-email limiter
        j = await login(client, rt, email, device=str(uuid.uuid4()))
        ids.add(j["user_id"])
        rt.mailer.sent.clear()
    assert len(ids) == 1
    async with rt.pg.connection() as conn:
        n = await (await conn.execute("SELECT count(*) AS n FROM defence.identities")).fetchone()
        u = await (await conn.execute("SELECT email FROM public.users")).fetchone()
    assert n["n"] == 1 and u["email"] == "joe@gmail.com"  # A's table stores the canonical form too


# ------------------------------------------------------------------ rejections
@pytest.mark.parametrize(
    "email,reason",
    [
        ("ann@gmail.org", "domain_not_allowed"),
        ("ann@example-college.edu.evil.com", "domain_not_allowed"),
        ("not-an-email", "malformed"),
        ("ann@mailinator.com", "domain_not_allowed"),  # not on the allowlist at all
    ],
)
async def test_bad_addresses_are_validation_errors(client, rt, email, reason):
    r = await register(client, email)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    assert r.json()["details"]["errors"][0]["reason"] == reason
    assert rt.mailer.sent == []


async def test_disposable_domains_blocked_even_when_allowlist_is_open(rt, client, settings_env, pg_dsn):
    settings_env(
        DATABASE_URL=pg_dsn, JWT_SECRET="t" * 40, ALLOWED_EMAIL_DOMAINS="*", AUTH_MODE="jwt", FD_ENV="dev"
    )
    r = await register(client, "bot@mailinator.com")
    assert r.status_code == 422 and r.json()["details"]["errors"][0]["reason"] == "disposable_domain"
    assert (await register(client, "human@anywhere.org")).status_code == 202


async def test_honeypot_looks_like_success_but_creates_and_sends_nothing(rt, client):
    r = await register(client, hp="http://spam.example")
    assert r.status_code == 202 and r.json()["status"] == "otp_sent"
    await asyncio.sleep(0.05)
    assert rt.mailer.sent == []
    async with rt.pg.connection() as conn:
        n = await (await conn.execute("SELECT count(*) AS n FROM defence.pending_registrations")).fetchone()
    assert n["n"] == 0


async def test_honeypot_response_is_indistinguishable_even_for_invalid_email(rt, client):
    ok = await register(client, hp="x")
    bad = await register(client, email="garbage", hp="x")
    assert ok.status_code == bad.status_code == 202
    assert set(ok.json()) == set(bad.json())


async def test_display_name_validation(client):
    assert (await register(client, name="   ")).status_code == 422
    assert (await register(client, name="x" * 81)).status_code == 422
    assert (await register(client, name="bad\x00name")).status_code == 422


# ------------------------------------------------------------------ OTP rules
async def test_wrong_code_then_right_code_works_and_code_is_single_use(rt, client):
    await register(client)
    await asyncio.sleep(0.05)
    good = rt.mailer.last_otp()
    wrong = "000000" if good != "000000" else "111111"
    r = await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": wrong})
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
    assert (await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": good})).status_code == 200
    again = await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": good})
    assert again.status_code == 401  # consumed


async def test_five_wrong_attempts_lock_the_code_even_for_the_right_one(rt, client):
    await register(client)
    await asyncio.sleep(0.05)
    good = rt.mailer.last_otp()
    wrong = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert (await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": wrong})).status_code == 401
    assert (await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": good})).status_code == 401


async def test_failed_attempts_persist_despite_the_error_response(rt, client):
    await register(client)
    await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": "000001"})
    async with rt.pg.connection() as conn:
        row = await (await conn.execute("SELECT attempts FROM defence.pending_registrations")).fetchone()
    assert row["attempts"] == 1  # raising must not roll the counter back


async def test_expired_code_is_rejected(rt, client):
    await register(client)
    await asyncio.sleep(0.05)
    good = rt.mailer.last_otp()
    async with rt.pg.connection() as conn:
        await conn.execute("UPDATE defence.pending_registrations SET expires_at = now() - interval '1 second'")
    assert (await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": good})).status_code == 401


async def test_new_registration_invalidates_the_previous_code(rt, client):
    await register(client)
    await asyncio.sleep(0.05)
    old = rt.mailer.last_otp()
    await register(client)
    await asyncio.sleep(0.05)
    new = rt.mailer.last_otp()
    if old != new:  # 1-in-a-million collision otherwise
        assert (await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": old})).status_code == 401
    assert (await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": new})).status_code == 200


async def test_unknown_email_and_wrong_code_are_indistinguishable(rt, client):
    await register(client)
    a = await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": "000000"})
    b = await client.post("/auth/verify", json={"email": "nobody@example-college.edu", "otp": "000000"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


async def test_malformed_code_is_422_and_not_charged(rt, client):
    await register(client)
    r = await client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": "12ab"})
    assert r.status_code == 422
    async with rt.pg.connection() as conn:
        row = await (await conn.execute("SELECT attempts FROM defence.pending_registrations")).fetchone()
    assert row["attempts"] == 0


async def test_concurrent_verifies_consume_the_code_exactly_once(rt, client):
    await register(client)
    await asyncio.sleep(0.05)
    good = rt.mailer.last_otp()
    rs = await asyncio.gather(*[client.post("/auth/verify", json={"email": "ann@example-college.edu", "otp": good}) for _ in range(8)])
    assert sorted(r.status_code for r in rs).count(200) == 1
    async with rt.pg.connection() as conn:
        n = await (await conn.execute("SELECT count(*) AS n FROM public.users")).fetchone()
    assert n["n"] == 1


# ---------------------------------------------------------------- rate limits
async def test_per_email_register_limit_returns_the_429_contract(rt, client):
    cap = int(limits.REGISTER["email"].capacity)
    for _ in range(cap):
        assert (await register(client)).status_code == 202
    r = await register(client)
    assert r.status_code == 429
    b = r.json()
    assert b["code"] == "RATE_LIMITED" and b["details"]["scope"] == "email" and b["details"]["retry_after_ms"] > 0
    assert int(r.headers["Retry-After"]) >= 1


async def test_per_device_register_limit(rt, client):
    dev = str(uuid.uuid4())
    cap = int(limits.REGISTER["device"].capacity)
    for i in range(cap):
        assert (await register(client, f"user{i}x@example-college.edu", device=dev)).status_code == 202
    r = await register(client, "another@example-college.edu", device=dev)
    assert r.status_code == 429 and r.json()["details"]["scope"] == "device"
    # A different device on the same IP is unaffected (per-IP limits are generous: shared campus NAT).
    assert (await register(client, "another@example-college.edu", device=str(uuid.uuid4()))).status_code == 202


async def test_garbage_device_id_is_ignored_not_trusted(rt, client):
    for i in range(8):  # more than the device bucket allows: would 429 if "garbage" were a key
        r = await register(client, f"g{i}abc@example-college.edu", headers={"X-Device-Id": "garbage"})
        assert r.status_code == 202


async def test_register_degrades_to_the_local_limiter_when_redis_is_down(rt, client):
    """Registration keeps working without Redis AND keeps a (weaker) limit: the per-process fallback."""

    class Dead:
        def register_script(self, _):
            async def boom(**_kw):
                raise ConnectionError("down")

            return boom

    from app.defence.ratelimit.bucket import RedisLimiter

    rt.limiter = RedisLimiter(Dead())
    codes = [(await register(client)).status_code for _ in range(5)]
    assert codes == [202, 202, 202, 429, 429]  # the email bucket (3) is still enforced, locally
    r = await register(client)
    assert r.json()["details"]["scope"] == "email" and int(r.headers["retry-after"]) >= 1
    assert (await register(client, "someone.else@example-college.edu", device=str(uuid.uuid4()))).status_code == 202  # others unaffected


# --------------------------------------------------------------- sequential flags
async def test_sequential_pattern_flag_trips_after_threshold(rt, client):
    for i in range(1, 8):
        await register(client, f"studentbot{i}@example-college.edu", device=str(uuid.uuid4()))
    async with rt.pg.connection() as conn:
        rows = await (await conn.execute("SELECT email_canonical, email_flags FROM defence.pending_registrations ORDER BY requested_at")).fetchall()
    flagged = [r["email_canonical"] for r in rows if r["email_flags"]["sequential_pattern"]]
    assert flagged == [f"studentbot{i}@example-college.edu" for i in range(5, 8)]  # 5th, 6th, 7th
    assert rows[-1]["email_flags"]["pattern_cluster_size"] == 7


async def test_roll_numbers_are_not_a_sequential_pattern(rt, client):
    for i in range(1, 8):
        await register(client, f"20210{i:02d}@example-college.edu", device=str(uuid.uuid4()))
    async with rt.pg.connection() as conn:
        rows = await (await conn.execute("SELECT email_flags FROM defence.pending_registrations")).fetchall()
    assert not any(r["email_flags"]["sequential_pattern"] for r in rows)


# ---------------------------------------------------------------- tokens / auth
async def test_refresh_issues_new_token_keeping_auth_time(rt, client):
    j = await login(client, rt)
    old = tokens.decode(j["token"])
    await asyncio.sleep(1.1)
    r = await client.post("/auth/refresh", headers={"Authorization": f"Bearer {j['token']}"})
    assert r.status_code == 200
    new = tokens.decode(r.json()["token"])
    assert new.user_id == old.user_id and new.auth_time == old.auth_time and new.exp > old.exp


async def test_expired_token_is_401_with_reason_but_refreshable(rt, client):
    j = await login(client, rt)
    now = int(time.time())
    stale, _ = tokens.mint(j["user_id"], email="ann@example-college.edu", name="Ann", now=now - 4000, ttl_s=1800)
    h = {"Authorization": f"Bearer {stale}"}
    me = await client.get("/auth/me", headers=h)
    assert me.status_code == 401 and me.json()["details"]["reason"] == "expired"
    ref = await client.post("/auth/refresh", headers=h)
    assert ref.status_code == 200
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {ref.json()['token']}"})).status_code == 200


async def test_forged_and_missing_tokens(rt, client):
    j = await login(client, rt)
    forged = pyjwt.encode({"iss": "fairdrop", "sub": j["user_id"], "iat": int(time.time()), "exp": int(time.time()) + 600}, "w" * 40, algorithm="HS256")
    for h in ({"Authorization": f"Bearer {forged}"}, {"Authorization": "Bearer junk"}, {}, {"Authorization": "Basic abc"}):
        r = await client.get("/auth/me", headers=h)
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
    assert (await client.post("/auth/refresh", headers={"Authorization": f"Bearer {forged}"})).status_code == 401


async def test_x_user_id_is_ignored_in_jwt_mode_without_simulation(rt, client):
    j = await login(client, rt)
    r = await client.get("/auth/me", headers={"X-User-Id": j["user_id"]})
    assert r.status_code == 401  # identity header alone must never authenticate
    r = await client.get("/auth/me", headers={"X-User-Id": j["user_id"], "X-Sim-Key": "anything"})
    assert r.status_code == 401


async def test_x_user_id_does_not_override_a_valid_token(rt, client):
    a = await login(client, rt, "ann@example-college.edu")
    other = str(uuid.uuid4())
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {a['token']}", "X-User-Id": other})
    assert r.json()["user_id"] == a["user_id"]


# ---------------------------------------------------------- simulation support
@pytest.fixture
def sim_on(settings_env, pg_dsn, rt):
    settings_env(
        DATABASE_URL=pg_dsn, JWT_SECRET="t" * 40, ADMIN_TOKEN="adm", AUTH_MODE="jwt", FD_ENV="dev",
        SIMULATION_MODE="true", SIM_KEY="simkey", ALLOWED_EMAIL_DOMAINS=DOMAIN, TRUSTED_PROXIES="127.0.0.1/32",
    )


async def test_otp_is_echoed_only_to_a_simulation_client_with_the_key(sim_on, client):
    plain = await register(client, "p1@example-college.edu", device=str(uuid.uuid4()))
    wrong = await register(client, "p2@example-college.edu", device=str(uuid.uuid4()), headers={"X-Sim-Key": "nope"})
    good = await register(client, "p3@example-college.edu", device=str(uuid.uuid4()), headers={"X-Sim-Key": "simkey"})
    assert "otp" not in plain.json() and "otp" not in wrong.json()
    otp = good.json()["otp"]
    assert len(otp) == 6
    assert (await client.post("/auth/verify", json={"email": "p3@example-college.edu", "otp": otp})).status_code == 200


async def test_sim_identity_header_works_in_jwt_mode_only_with_key(sim_on, client):
    uid = str(uuid.uuid4())
    ok = await client.get("/auth/me", headers={"X-User-Id": uid, "X-Sim-Key": "simkey"})
    assert ok.status_code == 200 and ok.json()["user_id"] == uid and ok.json()["auth"] == "sim"
    assert (await client.get("/auth/me", headers={"X-User-Id": uid, "X-Sim-Key": "bad"})).status_code == 401


async def test_sim_token_minting_requires_admin_and_simulation_mode(sim_on, client, settings_env, pg_dsn):
    ids = [str(uuid.uuid4()) for _ in range(3)]
    assert (await client.post("/admin/sim/tokens", json={"user_ids": ids})).status_code == 401
    assert (await client.post("/admin/sim/tokens", json={"user_ids": ids}, headers={"X-Admin-Token": "wrong"})).status_code == 403
    r = await client.post("/admin/sim/tokens", json={"user_ids": ids, "ttl_s": 600}, headers={"X-Admin-Token": "adm"})
    assert r.status_code == 200
    toks = r.json()["tokens"]
    assert set(toks) == set(ids)
    for uid, t in toks.items():  # real, verifiable tokens
        assert str(tokens.decode(t).user_id) == uid
        assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {t}"})).status_code == 200
    # outside simulation mode the route does not exist
    settings_env(DATABASE_URL=pg_dsn, JWT_SECRET="t" * 40, ADMIN_TOKEN="adm", AUTH_MODE="jwt", FD_ENV="dev")
    assert (await client.post("/admin/sim/tokens", json={"user_ids": ids}, headers={"X-Admin-Token": "adm"})).status_code == 404


async def test_sim_token_request_limits(sim_on, client):
    h = {"X-Admin-Token": "adm"}
    assert (await client.post("/admin/sim/tokens", json={"user_ids": []}, headers=h)).status_code == 422
    assert (await client.post("/admin/sim/tokens", json={"user_ids": ["nope"]}, headers=h)).status_code == 422
    assert (await client.post("/admin/sim/tokens", json={"user_ids": [str(uuid.uuid4())], "ttl_s": 5}, headers=h)).status_code == 422


# --------------------------------------------------- end to end with the stub API
async def test_full_flow_register_verify_enter_event(rt, client, pg_dsn):
    """Real token -> the stub's /enter (gate is a pass-through until stage 5)."""
    from app import db

    await db.open_pool()
    try:
        await db.init_schema()
        j = await login(client, rt)
        h = {"Authorization": f"Bearer {j['token']}"}
        ev = "11111111-1111-1111-1111-111111111111"
        first = await client.post(f"/events/{ev}/enter", headers=h)
        again = await client.post(f"/events/{ev}/enter", headers=h)
        assert first.status_code == 201 and again.status_code == 200 and again.json()["already_entered"] is True
        # a validly signed token for a user that does not exist is a 401, not a 500
        ghost, _ = tokens.mint(uuid.uuid4())
        assert (await client.post(f"/events/{ev}/enter", headers={"Authorization": f"Bearer {ghost}"})).status_code == 401
        assert (await client.post(f"/events/{ev}/enter")).status_code == 401
    finally:
        await db.close_pool()
