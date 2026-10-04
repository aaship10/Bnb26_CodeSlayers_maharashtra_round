"""The challenge flow through the real stub app, Postgres and Redis (or fakeredis)."""
import time
import uuid

import httpx
import pytest
from app.defence import gate as gate_mod
from app.defence.captcha import providers
from app.defence.captcha.providers import MOCK_UI_TOKEN, ProviderUnavailable, TurnstileCaptcha, mint_sim_token
from app.defence.pow import protocol
from app.defence.settings import get_settings

EV = "11111111-1111-1111-1111-111111111111"
EVU = uuid.UUID(EV)


@pytest.fixture(autouse=True)
def fresh_providers():
    gate_mod._providers.clear()
    yield
    gate_mod._providers.clear()


async def enter(client, headers, solution=None):
    h = dict(headers)
    if solution:
        h["X-Challenge-Id"], h["X-Challenge-Solution"] = solution
    return await client.post(f"/events/{EV}/enter", headers=h)


def challenge_of(r):
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "CHALLENGE_REQUIRED"
    return r.json()["details"]["challenge"]


def solve(ch):
    return ch["id"], protocol.solve(ch["pow"]["prefix"], ch["pow"]["difficulty_bits"])


async def entries(rt, uid):
    async with rt.pg.connection() as conn:
        return (await (await conn.execute("SELECT count(*) AS n FROM entries WHERE user_id = %s", (uid,))).fetchone())["n"]


# ----------------------------------------------------------------------- layers off / PoW
async def test_no_layers_means_no_challenge(stub, client):
    _, h = await stub.user()
    assert (await enter(client, h)).status_code == 201


async def test_pow_challenge_has_the_contract_shape(stub, client):
    await stub.set_defences({"preset": "rate_limit+pow"})
    uid, h = await stub.user()
    r = await enter(client, h)
    ch = challenge_of(r)
    assert set(ch) == {"id", "type", "expires_at", "pow"} and ch["type"] == "pow"
    assert ch["pow"]["algo"] == "sha256-lzb" and ch["pow"]["prefix"] == ch["id"]  # the id IS the prefix
    assert ch["pow"]["difficulty_bits"] == 14 and ch["expires_at"].endswith("Z")
    assert len(ch["id"]) <= 130
    st, tok = protocol.check(ch["id"], get_settings().pow_secret, uid, EVU, int(time.time()))
    assert st is protocol.Status.OK and tok.bits == 14  # bound to this user and event


async def test_solve_and_enter_then_the_entry_is_one_row(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow"})
    uid, h = await stub.user()
    cid, nonce = solve(challenge_of(await enter(client, h)))
    r = await enter(client, h, (cid, nonce))
    assert r.status_code == 201 and r.json()["state"] == "ENTERED"
    assert await entries(rt, uid) == 1
    async with rt.pg.connection() as conn:
        row = await (await conn.execute("SELECT weight, risk FROM entries WHERE user_id = %s", (uid,))).fetchone()
    assert float(row["weight"]) == 1.0 and row["risk"] == {"challenges_passed": ["pow"]}


async def test_repeat_enter_after_success_costs_no_challenge(stub, client):
    await stub.set_defences({"preset": "rate_limit+pow"})
    _, h = await stub.user()
    await enter(client, h, solve(challenge_of(await enter(client, h))))
    again = await enter(client, h)  # no headers: A's fast path runs before the gate
    assert again.status_code == 200 and again.json()["already_entered"] is True


async def test_wrong_nonce_gets_a_fresh_challenge_not_a_lockout(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow"})
    uid, h = await stub.user()
    ch = challenge_of(await enter(client, h))
    bad = next(str(n) for n in range(10_000) if not protocol.solution_ok(ch["id"], str(n), 14))
    r = await enter(client, h, (ch["id"], bad))
    ch2 = challenge_of(r)
    assert ch2["id"] != ch["id"] and await entries(rt, uid) == 0
    assert (await enter(client, h, solve(ch2))).status_code == 201  # and they can still get in


@pytest.mark.parametrize("junk", ["", "abc", "-1", "1" * 25, "1.5"])
async def test_malformed_solutions_get_a_fresh_challenge(stub, client, junk):
    await stub.set_defences({"preset": "rate_limit+pow"})
    _, h = await stub.user()
    ch = challenge_of(await enter(client, h))
    challenge_of(await enter(client, h, (ch["id"], junk)))


async def test_a_solution_is_single_use(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow"})
    uid, h = await stub.user()
    sol = solve(challenge_of(await enter(client, h)))
    assert (await enter(client, h, sol)).status_code == 201
    async with rt.pg.connection() as conn:  # simulate the entry never having been recorded
        await conn.execute("DELETE FROM entries WHERE user_id = %s", (uid,))
    replay = await enter(client, h, sol)
    assert challenge_of(replay)["id"] != sol[0]  # consumed: a NEW challenge, not an entry
    assert await entries(rt, uid) == 0


async def test_someone_elses_solved_challenge_is_useless_to_me(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow"})
    (_, ha), (ub, hb) = await stub.user(), await stub.user()
    sol = solve(challenge_of(await enter(client, ha)))  # A solves their challenge...
    r = await enter(client, hb, sol)  # ...B replays it
    assert challenge_of(r)["id"] != sol[0] and await entries(rt, ub) == 0


async def test_expired_challenge_gets_a_fresh_one(stub, client):
    await stub.set_defences({"preset": "rate_limit+pow"})
    uid, h = await stub.user()
    old = protocol.mint(get_settings().pow_secret, "p", EVU, uid, int(time.time()) - 5, 8)
    r = await enter(client, h, (old, protocol.solve(old, 8)))
    assert challenge_of(r)["id"] != old


# ----------------------------------------------------------------------------- forgery
@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace(".14.", ".0.", 1),  # lower the difficulty
        lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),
        lambda t: "garbage",
        lambda t: "fd1.p0." + "0" * 32 + "." + "0" * 32 + ".0.0." + "0" * 16 + "." + "0" * 24,
    ],
)
async def test_forged_tokens_are_rejected_not_challenged(stub, client, rt, mutate):
    await stub.set_defences({"preset": "rate_limit+pow"})
    uid, h = await stub.user()
    ch = challenge_of(await enter(client, h))
    r = await enter(client, h, (mutate(ch["id"]), "0"))
    assert r.status_code == 403 and r.json()["code"] == "REJECTED"
    assert await entries(rt, uid) == 0


async def test_a_token_signed_with_another_key_cannot_pass(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow"})
    uid, h = await stub.user()
    fake = protocol.mint("attacker-key", "p", EVU, uid, int(time.time()) + 60, 0)  # difficulty 0, perfectly formed
    r = await enter(client, h, (fake, "0"))
    assert r.json()["code"] == "REJECTED" and await entries(rt, uid) == 0


# ------------------------------------------------------------------- PoW + CAPTCHA chain
async def test_pow_then_captcha_with_one_solution_header_each(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow+captcha"})
    uid, h = await stub.user()
    pow_ch = challenge_of(await enter(client, h))
    assert pow_ch["type"] == "pow"
    cap = challenge_of(await enter(client, h, solve(pow_ch)))  # PoW done -> now the CAPTCHA
    assert cap["type"] == "captcha" and cap["captcha"] == {"provider": "mock", "site_key": ""}
    assert ".c1." in cap["id"]  # carries the signed "PoW already passed" bit
    r = await enter(client, h, (cap["id"], MOCK_UI_TOKEN))
    assert r.status_code == 201 and await entries(rt, uid) == 1
    async with rt.pg.connection() as conn:
        row = await (await conn.execute("SELECT risk FROM entries WHERE user_id = %s", (uid,))).fetchone()
    assert row["risk"] == {"challenges_passed": ["pow", "captcha"]}


async def test_wrong_captcha_token_gets_a_new_captcha_not_a_new_pow(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow+captcha"})
    uid, h = await stub.user()
    cap = challenge_of(await enter(client, h, solve(challenge_of(await enter(client, h)))))
    again = challenge_of(await enter(client, h, (cap["id"], "not-a-valid-token")))
    assert again["type"] == "captcha" and ".c1." in again["id"] and again["id"] != cap["id"]  # no PoW redo
    assert await entries(rt, uid) == 0


async def test_captcha_without_the_pow_bit_sends_you_back_to_pow(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow+captcha"})
    uid, h = await stub.user()
    skip = protocol.mint(get_settings().pow_secret, "c", EVU, uid, int(time.time()) + 60, 0, pow_ok=False)  # a validly signed c0
    r = challenge_of(await enter(client, h, (skip, MOCK_UI_TOKEN)))
    assert r["type"] == "pow" and await entries(rt, uid) == 0  # a good CAPTCHA does not replace the PoW


async def test_the_pow_ok_bit_cannot_be_flipped_by_the_client(stub, client):
    await stub.set_defences({"preset": "rate_limit+pow+captcha"})
    uid, h = await stub.user()
    c0 = protocol.mint(get_settings().pow_secret, "c", EVU, uid, int(time.time()) + 60, 0, pow_ok=False)
    r = await enter(client, h, (c0.replace(".c0.", ".c1.", 1), MOCK_UI_TOKEN))
    assert r.json()["code"] == "REJECTED"


async def test_captcha_only_config(stub, client, rt):
    await stub.set_defences({"preset": "custom", "layers": {"captcha": {"enabled": True, "mode": "always"}}})
    uid, h = await stub.user()
    cap = challenge_of(await enter(client, h))
    assert cap["type"] == "captcha"
    assert (await enter(client, h, (cap["id"], MOCK_UI_TOKEN))).status_code == 201


async def test_sim_captcha_tokens_model_a_solving_service(stub, client, settings_env, pg_dsn):
    settings_env(DATABASE_URL=pg_dsn, JWT_SECRET="t" * 40, ADMIN_TOKEN="adm", AUTH_MODE="jwt", FD_ENV="prod",
                 MOCK_CAPTCHA_UI="false", SIMULATION_MODE="true", SIM_KEY="simkey", TRUSTED_PROXIES="127.0.0.1/32",
                 SMTP_HOST="localhost")
    gate_mod._providers.clear()
    await stub.set_defences({"preset": "custom", "layers": {"captcha": {"enabled": True, "mode": "always"}}})
    uid, h = await stub.user()
    cap = challenge_of(await enter(client, h))
    assert challenge_of(await enter(client, h, (cap["id"], MOCK_UI_TOKEN)))["type"] == "captcha"  # demo token dead in prod
    cap = challenge_of(await enter(client, h))
    assert (await enter(client, h, (cap["id"], mint_sim_token("simkey", uid, EVU)))).status_code == 201


# ------------------------------------------------------- accessible fallback (waivers)
async def test_waiver_skips_the_captcha_but_not_the_pow(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow+captcha"})
    uid, h = await stub.user()
    adm = {"X-Admin-Token": "adm"}
    assert (await client.post("/admin/defence/waivers", json={"event_id": EV, "user_id": str(uid)})).status_code == 401  # admin only
    r = await client.post("/admin/defence/waivers", json={"event_id": EV, "user_id": str(uid), "reason": "screen reader"}, headers=adm)
    assert r.status_code == 201 and r.json()["waived"] == ["captcha"]
    pow_ch = challenge_of(await enter(client, h))
    assert pow_ch["type"] == "pow"  # PoW still applies
    assert (await enter(client, h, solve(pow_ch))).status_code == 201  # ...and no CAPTCHA after it
    listing = await client.get(f"/admin/defence/waivers?event_id={EV}", headers=adm)
    assert listing.json()[0]["reason"] == "screen reader"


async def test_waiver_is_per_person_and_revocable(stub, client):
    await stub.set_defences({"preset": "rate_limit+pow+captcha"})
    (ua, ha), (_, hb) = await stub.user(), await stub.user()
    adm = {"X-Admin-Token": "adm"}
    await client.post("/admin/defence/waivers", json={"event_id": EV, "user_id": str(ua)}, headers=adm)
    cap_b = challenge_of(await enter(client, hb, solve(challenge_of(await enter(client, hb)))))
    assert cap_b["type"] == "captcha"  # B was not waived
    assert (await client.delete(f"/admin/defence/waivers?event_id={EV}&user_id={ua}", headers=adm)).status_code == 200
    cap_a = challenge_of(await enter(client, ha, solve(challenge_of(await enter(client, ha)))))
    assert cap_a["type"] == "captcha"  # revoked
    assert (await client.delete(f"/admin/defence/waivers?event_id={EV}&user_id={ua}", headers=adm)).status_code == 404


# ------------------------------------------------------------------- provider outage
async def test_provider_outage_is_a_retryable_503_not_a_lockout_or_a_free_pass(stub, client, rt, settings_env, pg_dsn):
    settings_env(DATABASE_URL=pg_dsn, JWT_SECRET="t" * 40, ADMIN_TOKEN="adm", AUTH_MODE="jwt", FD_ENV="dev",
                 TRUSTED_PROXIES="127.0.0.1/32", CAPTCHA_SECRET="s")
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    gate_mod._providers["turnstile"] = TurnstileCaptcha("s", client=down)
    await stub.set_defences({"preset": "custom", "layers": {"captcha": {"enabled": True, "mode": "always", "provider": "turnstile", "site_key": "1x00000000000000000000AA"}}})
    uid, h = await stub.user()
    cap = challenge_of(await enter(client, h))
    assert cap["captcha"] == {"provider": "turnstile", "site_key": "1x00000000000000000000AA"}
    r = await enter(client, h, (cap["id"], "some-turnstile-token"))
    assert r.status_code == 503 and r.headers["retry-after"] == "3" and r.json()["details"]["scope"] == "captcha"
    assert await entries(rt, uid) == 0
    # the provider recovers; the person simply tries again with a new challenge
    gate_mod._providers["turnstile"] = TurnstileCaptcha("s", client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"success": True}))))
    cap2 = challenge_of(await enter(client, h))
    assert (await enter(client, h, (cap2["id"], "tok"))).status_code == 201


async def test_turnstile_without_a_secret_is_a_clear_503(stub, client, settings_env, pg_dsn):
    settings_env(DATABASE_URL=pg_dsn, JWT_SECRET="t" * 40, ADMIN_TOKEN="adm", AUTH_MODE="jwt", FD_ENV="dev", TRUSTED_PROXIES="127.0.0.1/32")
    await stub.set_defences({"preset": "custom", "layers": {"captcha": {"enabled": True, "mode": "always", "provider": "turnstile", "site_key": "k"}}})
    _, h = await stub.user()
    cap = challenge_of(await enter(client, h))
    assert (await enter(client, h, (cap["id"], "tok"))).status_code == 503


# ------------------------------------------------------------------------- adaptive load
async def test_load_raises_difficulty_within_the_cap(stub, client, rt):
    await stub.set_defences({"preset": "custom", "layers": {"pow": {"enabled": True, "mode": "always", "base_bits": 10,
                                                                       "load_bits_max": 2, "load_ref_rps": 10, "max_bits": 12}}})
    _, h = await stub.user()
    assert challenge_of(await enter(client, h))["pow"]["difficulty_bits"] == 10  # idle
    sec = int(time.time())
    for s in range(sec - 2, sec + 1):  # a heavy previous second (set a few so a clock tick cannot miss it)
        await rt.redis.set(f"fd:{get_settings().env}:load:{EV}:{s}", 500, ex=30)
    _, h2 = await stub.user()
    assert challenge_of(await enter(client, h2))["pow"]["difficulty_bits"] == 12  # +2 at full load, capped at max_bits


# --------------------------------------------------------------- failure policy / pre-fetch
async def test_single_use_tracking_is_skipped_when_redis_is_down(stub, client, rt):
    from app.defence.ratelimit.bucket import RedisLimiter

    class Dead:
        def register_script(self, _):
            async def boom(**_k):
                raise ConnectionError("down")

            return boom

        def pipeline(self, **_k):
            raise ConnectionError("down")

        async def set(self, *a, **k):
            raise ConnectionError("down")

    await stub.set_defences({"preset": "rate_limit+pow"})
    rt.redis, rt.limiter = Dead(), RedisLimiter(Dead())
    _, h = await stub.user()
    assert (await enter(client, h, solve(challenge_of(await enter(client, h))))).status_code == 201  # still works


async def test_prefetch_endpoint(stub, client):
    _, h = await stub.user()
    r = await client.post("/defence/challenge", json={"event_id": EV}, headers=h)
    assert r.status_code == 404 and r.json()["details"]["reason"] == "no_challenge_required"
    await stub.set_defences({"preset": "rate_limit+pow"})
    r = await client.post("/defence/challenge", json={"event_id": EV}, headers=h)
    assert r.status_code == 200 and r.json()["type"] == "pow" and r.json()["server_now"].endswith("Z")
    ch = r.json()
    assert (await enter(client, h, solve(ch))).status_code == 201  # a pre-fetched challenge is usable
    assert (await client.post("/defence/challenge", json={"event_id": EV})).status_code == 401  # needs identity
    assert (await client.post("/defence/challenge", json={"event_id": "nope"}, headers=h)).status_code == 422


async def test_prefetch_is_rate_limited(stub, client):
    await stub.set_defences({"preset": "custom", "layers": {"pow": {"enabled": True, "mode": "always"},
                                                            "rate_limit": {"enabled": True, "limits": {"challenge": {"identity": {"capacity": 2, "refill_per_s": 0.001}}}}}})
    _, h = await stub.user()
    codes = [(await client.post("/defence/challenge", json={"event_id": EV}, headers=h)).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]


async def test_risk_mode_layers_do_not_challenge_before_the_risk_engine_exists(stub, client):
    await stub.set_defences({"preset": "all"})  # pow always, captcha in risk mode
    _, h = await stub.user()
    ch = challenge_of(await enter(client, h))
    assert ch["type"] == "pow"
    cap_or_in = await enter(client, h, solve(ch))
    assert cap_or_in.status_code == 201  # captcha(risk) is not asked of anyone yet (stage 5)
