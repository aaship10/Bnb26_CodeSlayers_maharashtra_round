import time
import uuid

import httpx
import pytest
from app.defence.captcha.providers import (
    MOCK_UI_TOKEN,
    MockCaptcha,
    ProviderUnavailable,
    TurnstileCaptcha,
    build_provider,
    mint_sim_token,
)
from app.defence.settings import get_settings

U, E = uuid.UUID(int=7), uuid.UUID(int=8)


def verify(p, token, ip=None):
    return p.verify(token, user_id=U, event_id=E, remote_ip=ip)


# -------------------------------------------------------------------------------- mock
async def test_mock_ui_token_only_when_enabled(settings_env):
    settings_env(FD_ENV="dev")
    assert await verify(MockCaptcha(get_settings()), MOCK_UI_TOKEN) is True
    settings_env(FD_ENV="prod", MOCK_CAPTCHA_UI="false")
    assert await verify(MockCaptcha(get_settings()), MOCK_UI_TOKEN) is False  # the demo token is useless in production
    settings_env(FD_ENV="prod", MOCK_CAPTCHA_UI="true")
    assert await verify(MockCaptcha(get_settings()), MOCK_UI_TOKEN) is True  # explicit opt-in only


async def test_mock_rejects_everything_else(settings_env):
    settings_env(FD_ENV="dev")
    p = MockCaptcha(get_settings())
    for t in ("", "nope", MOCK_UI_TOKEN + "x", "x" * 5000, "sim1.garbage"):
        assert await verify(p, t) is False


async def test_sim_tokens_need_simulation_mode_and_the_key(settings_env):
    tok = mint_sim_token("simkey", U, E)
    settings_env(FD_ENV="prod", MOCK_CAPTCHA_UI="false", SIMULATION_MODE="true", SIM_KEY="simkey")
    assert await verify(MockCaptcha(get_settings()), tok) is True
    settings_env(FD_ENV="prod", MOCK_CAPTCHA_UI="false", SIMULATION_MODE="false", SIM_KEY="simkey")
    assert await verify(MockCaptcha(get_settings()), tok) is False  # mode off: a valid token means nothing
    settings_env(FD_ENV="prod", MOCK_CAPTCHA_UI="false", SIMULATION_MODE="true", SIM_KEY="different")
    assert await verify(MockCaptcha(get_settings()), tok) is False  # signed with another key


async def test_sim_tokens_are_bound_and_expire(settings_env):
    settings_env(FD_ENV="prod", MOCK_CAPTCHA_UI="false", SIMULATION_MODE="true", SIM_KEY="simkey")
    p = MockCaptcha(get_settings())
    assert await p.verify(mint_sim_token("simkey", U, E), user_id=uuid.UUID(int=99), event_id=E, remote_ip=None) is False
    assert await p.verify(mint_sim_token("simkey", U, E), user_id=U, event_id=uuid.UUID(int=99), remote_ip=None) is False
    old = mint_sim_token("simkey", U, E, ts=int(time.time()) - 301)
    assert await verify(p, old) is False
    good = mint_sim_token("simkey", U, E)
    tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert await verify(p, good) is True and await verify(p, tampered) is False


# ---------------------------------------------------------------------------- turnstile
def transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_turnstile_posts_the_documented_form_and_reads_success():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["form"] = dict(httpx.QueryParams(req.content.decode()))
        seen["ctype"] = req.headers["content-type"]
        return httpx.Response(200, json={"success": True, "error-codes": []})

    p = TurnstileCaptcha("sekrit", client=transport(handler))
    assert await verify(p, "tok123", ip="203.0.113.5") is True
    assert seen["url"] == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    assert seen["form"] == {"secret": "sekrit", "response": "tok123", "remoteip": "203.0.113.5"}
    assert seen["ctype"].startswith("application/x-www-form-urlencoded")


async def test_turnstile_rejection_is_false_not_an_error():
    p = TurnstileCaptcha("s", client=transport(lambda r: httpx.Response(200, json={"success": False, "error-codes": ["timeout-or-duplicate"]})))
    assert await verify(p, "tok") is False
    p = TurnstileCaptcha("s", client=transport(lambda r: httpx.Response(200, json={"success": "true"})))  # not the boolean True
    assert await verify(p, "tok") is False


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(503, text="down"),
        lambda r: httpx.Response(200, text="<html>not json</html>"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("no route")),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
    ],
)
async def test_provider_outage_is_distinguishable_from_a_rejected_token(handler):
    with pytest.raises(ProviderUnavailable):
        await verify(TurnstileCaptcha("s", client=transport(handler)), "tok")


async def test_turnstile_never_forwards_oversized_or_empty_tokens():
    called = False

    def handler(r):
        nonlocal called
        called = True
        return httpx.Response(200, json={"success": True})

    p = TurnstileCaptcha("s", client=transport(handler))
    assert await verify(p, "") is False and await verify(p, "x" * 2049) is False
    assert called is False


def test_secret_is_required_and_never_printed():
    with pytest.raises(ValueError):
        TurnstileCaptcha("")
    assert "sekrit" not in repr(TurnstileCaptcha("sekrit"))


def test_build_provider(settings_env):
    settings_env(CAPTCHA_SECRET="s")
    assert build_provider("mock", get_settings()).name == "mock"
    assert build_provider("turnstile", get_settings()).name == "turnstile"
    with pytest.raises(ValueError):
        build_provider("hcaptcha", get_settings())
    settings_env()
    with pytest.raises(ValueError):
        build_provider("turnstile", get_settings())  # no secret configured


# ------------------------------------------------- the real Cloudflare endpoint (dummy keys)
async def test_real_turnstile_endpoint_with_cloudflare_test_keys():
    """Proves our request/response handling against the REAL endpoint using Cloudflare's documented
    dummy secrets (1x00..AA always passes, 2x00..AA always fails). Observed 2026-10: the always-pass
    secret accepts ANY token, although the docs page implies only the dummy token; so this test
    checks the contract (success flag, error shape), not that bad tokens are rejected.
    Skipped when the network is unreachable."""
    ok = TurnstileCaptcha("1x0000000000000000000000000000000AA")
    bad = TurnstileCaptcha("2x0000000000000000000000000000000AA")
    try:
        assert await verify(ok, "XXXX.DUMMY.TOKEN.XXXX") is True
    except ProviderUnavailable as exc:
        pytest.skip(f"Cloudflare unreachable: {exc}")
    assert await verify(bad, "XXXX.DUMMY.TOKEN.XXXX") is False
