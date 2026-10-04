"""B's SIM_KEY-signed CAPTCHA token: golden vectors from B's own mint_sim_token, the verifier, the solver
choosing it, and an end-to-end run where humans and bots clear a CAPTCHA defence with it."""
from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass

import pytest

from fairdrop_sim.challenge.captcha import (
    MOCK_CAPTCHA_TOKEN,
    SIM_TOKEN_TTL_S,
    CaptchaModel,
    hex32,
    mint_sim_token,
    verify_sim_token,
)
from fairdrop_sim.challenge.solver import Solver
from tests.test_engine_e2e import mock_url, scenario  # noqa: F401

# Produced by Member B's real function (Member_B_Pipeline/backend/app/defence/captcha/providers.py,
# origin/main 10277c8) on 2026-10-04. If B changes the token format these must be regenerated from B's code.
GOLDEN = [
    ("test-sim-key", "12345678-1234-5678-1234-567812345678", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", 1_800_000_000,
     "sim1.12345678123456781234567812345678.aaaaaaaabbbbccccddddeeeeeeeeeeee.1800000000.7c5880c6c31c2dfe95e98387b4ef0d69"),
    ("another-key", str(uuid.UUID(int=1)), str(uuid.UUID(int=2)), 1,
     "sim1.00000000000000000000000000000001.00000000000000000000000000000002.1.d4954a40f4ad4f263cfc20e280ba660b"),
]


@pytest.mark.parametrize("key,user,event,ts,expected", GOLDEN)
def test_token_is_byte_identical_to_bs_reference(key, user, event, ts, expected):
    assert mint_sim_token(key, user, event, ts) == expected
    assert verify_sim_token(expected, key, user, event, now=ts)


def test_verifier_rejects_everything_b_would_reject():
    key, user, event = "k", str(uuid.uuid4()), str(uuid.uuid4())
    now = 1_800_000_000
    tok = mint_sim_token(key, user, event, now)
    assert verify_sim_token(tok, key, user, event, now=now + SIM_TOKEN_TTL_S)  # last valid second
    assert not verify_sim_token(tok, key, user, event, now=now + SIM_TOKEN_TTL_S + 1)  # expired
    assert not verify_sim_token(tok, key, user, event, now=now - SIM_TOKEN_TTL_S - 1)  # from the future
    assert not verify_sim_token(tok, "other-key", user, event, now=now)  # forged: wrong key
    assert not verify_sim_token(tok, key, str(uuid.uuid4()), event, now=now)  # someone else's user
    assert not verify_sim_token(tok, key, user, str(uuid.uuid4()), now=now)  # another event
    for junk in ("", "sim1", "sim1.a.b.c.d", "mock-captcha-ok", tok + ".x", tok.replace("sim1", "sim2", 1)):
        assert not verify_sim_token(junk, key, user, event, now=now), junk
    assert not verify_sim_token(tok[:-1] + ("0" if tok[-1] != "0" else "1"), key, user, event, now=now)  # one bit off


def test_non_uuid_ids_are_hashed_consistently_on_both_ends():
    assert hex32("evt_sim_x") == hex32("evt_sim_x") and len(hex32("evt_sim_x")) == 32
    u = uuid.uuid4()
    assert hex32(str(u)) == u.hex  # real ids use the UUID's own hex, exactly like B's code
    assert verify_sim_token(mint_sim_token("k", str(u), "evt_sim_x"), "k", str(u), "evt_sim_x")


@dataclass
class Ident:
    user_id: str


def solve(solver: Solver, ident=None):
    ch = {"id": "ch_1", "type": "captcha", "captcha": {"provider": "mock", "site_key": "k"}}
    return asyncio.run(solver.solve(ch, random.Random(0), ident))


def test_solver_mints_the_signed_token_when_it_has_a_key_and_the_fixed_one_otherwise():
    model = CaptchaModel(solve_s_mean=0.0, fail_rate=0.0)
    u = str(uuid.uuid4())
    cid, tok = solve(Solver(captcha=model, sim_key="k", event_id="e1"), Ident(u))
    assert cid == "ch_1" and verify_sim_token(tok, "k", u, "e1")
    assert solve(Solver(captcha=model), Ident(u))[1] == MOCK_CAPTCHA_TOKEN  # no key: the demo token
    assert solve(Solver(captcha=model, sim_key="k", event_id="e1"), None)[1] == MOCK_CAPTCHA_TOKEN  # no identity


def test_a_token_for_one_user_is_useless_to_another():
    """Why the solver needs the identity: a bot cannot mint once and share the token across its farm."""
    model = CaptchaModel(solve_s_mean=0.0, fail_rate=0.0)
    s = Solver(captcha=model, sim_key="k", event_id="e1")
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    tok_a = solve(s, Ident(a))[1]
    assert verify_sim_token(tok_a, "k", a, "e1") and not verify_sim_token(tok_a, "k", b, "e1")


# --------------------------------------------------------------------------- end to end against the mock


def captcha_scenario(**over):
    return scenario(
        event={"inventory": 15, "mode": "LOTTERY", "window_seconds": 3,
               "defences": {"preset": "custom", "layers": {"captcha": {"enabled": True}}}},
        legit={"count": 80, "device": {"captcha_solve_s_mean": 0.05, "captcha_fail_rate": 0.0, "pow_mode": "real"}},
        load={"lead_s": 1.5, "claim_phase_s": 3},
        attackers=[{"profile": "human_mimic", "identities": 30, "ips": 30, "rps_per_identity": 2, "solves_captcha": True,
                    "captcha_solve_s_mean": 0.05, "captcha_fail_rate": 0.0},
                   {"profile": "sybil_single_ip", "identities": 30, "ips": 1, "rps_per_identity": 2}], **over)


def test_everyone_who_solves_clears_the_captcha_with_the_signed_token_and_a_non_solver_does_not(mock_url, tmp_path):  # noqa: F811
    from fairdrop_sim.engine.run import TargetConfig, execute_run

    s = asyncio.run(execute_run(captcha_scenario(), 0, TargetConfig(base_url=mock_url), out_dir=tmp_path, log=lambda _m: None))
    mimic = next(a for a in s["attackers"] if a["profile"] == "human_mimic")
    sybil = next(a for a in s["attackers"] if a["profile"] == "sybil_single_ip")
    assert mimic["challenges_seen"] >= 25 and mimic["server_entries"] >= 25  # cleared it, with real cost counted
    assert mimic["cost"]["totals"]["captcha_solves"] >= 25
    assert sybil["server_entries"] == 0 and sybil["cost"]["totals"]["captcha_solves"] == 0  # never solved
    assert s["outcomes"]["entered_server"] >= 70  # humans solved too
    assert s["integrity"]["passed"]
