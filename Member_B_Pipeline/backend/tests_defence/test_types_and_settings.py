from datetime import datetime, timedelta, timezone

import pytest
from app.defence.settings import get_settings
from app.defence.timeutil import iso_z
from app.defence.contracts import ALLOWED_WEIGHTS, Action, CaptchaParams, Challenge, GateDecision, PowParams


def _pow_challenge() -> Challenge:
    return Challenge(
        id="c1",
        type="pow",
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        pow=PowParams(prefix="p", difficulty_bits=12),
    )


@pytest.mark.parametrize("w", ALLOWED_WEIGHTS)
def test_allowed_weights_pass(w):
    assert GateDecision.allow(weight=w).weight == w


@pytest.mark.parametrize("w", [0.0, 0.1, 0.75, 1.5, -1.0, 2])
def test_other_weights_are_impossible(w):
    with pytest.raises(ValueError):
        GateDecision.allow(weight=w)


def test_non_allow_decisions_carry_no_weight():
    with pytest.raises(ValueError):
        GateDecision(Action.REJECT, weight=1.0)
    assert GateDecision.reject("honeypot", layer="identity").weight == 0.0


def test_challenge_decision_requires_a_challenge_and_only_it():
    with pytest.raises(ValueError):
        GateDecision(Action.CHALLENGE)
    with pytest.raises(ValueError):
        GateDecision(Action.ALLOW, weight=1.0, challenge=_pow_challenge())
    assert GateDecision.require_challenge(_pow_challenge()).action is Action.CHALLENGE


def test_challenge_wire_shape_matches_contract():
    d = _pow_challenge().to_dict()
    assert d == {
        "id": "c1",
        "type": "pow",
        "expires_at": "2030-01-01T00:00:00.000Z",
        "pow": {"algo": "sha256-lzb", "prefix": "p", "difficulty_bits": 12},
    }
    cap = Challenge("c2", "captcha", datetime(2030, 1, 1, tzinfo=timezone.utc), captcha=CaptchaParams("mock", "k"))
    assert "pow" not in cap.to_dict() and cap.to_dict()["captcha"] == {"provider": "mock", "site_key": "k"}


def test_challenge_type_must_match_payload():
    with pytest.raises(ValueError):
        Challenge("x", "pow", datetime.now(timezone.utc))


def test_iso_z_is_utc_with_z_and_rejects_naive():
    local = datetime(2030, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert iso_z(local) == "2030-01-01T00:00:00.000Z"
    with pytest.raises(ValueError):
        iso_z(datetime(2030, 1, 1))


def test_settings_defaults_are_safe(settings_env):
    settings_env()
    s = get_settings()
    assert s.auth_mode == "dev" and s.simulation_mode is False
    assert s.trusted_proxies == ()  # trust nobody by default
    assert s.allowed_email_domains == ("example-college.edu",)


def test_trusted_proxies_parse_and_reject_garbage(settings_env):
    settings_env(TRUSTED_PROXIES="127.0.0.1/32, 172.16.0.0/12")
    assert len(get_settings().trusted_proxies) == 2
    settings_env(TRUSTED_PROXIES="not-a-cidr")
    with pytest.raises(ValueError):
        get_settings()


def test_simulation_mode_requires_a_sim_key(settings_env):
    settings_env(SIMULATION_MODE="true")
    with pytest.raises(ValueError):
        get_settings()
    settings_env(SIMULATION_MODE="true", SIM_KEY="k")
    assert get_settings().simulation_mode is True


def test_jwt_mode_requires_a_strong_secret(settings_env):
    settings_env(AUTH_MODE="jwt", JWT_SECRET="short")
    with pytest.raises(ValueError):
        get_settings()
    settings_env(AUTH_MODE="jwt", JWT_SECRET="x" * 32)
    assert get_settings().auth_mode == "jwt"


def test_bad_auth_mode_is_rejected(settings_env):
    settings_env(AUTH_MODE="magic")
    with pytest.raises(ValueError):
        get_settings()
