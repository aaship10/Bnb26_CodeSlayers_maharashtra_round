"""Bot units: identities/IPs, behaviour overrides, the solver, and cost accounting."""
from __future__ import annotations

import asyncio
import random

import pytest

from fairdrop_sim.bots.cost import CostAccount
from fairdrop_sim.bots.profiles import PROFILES, SUGGESTED, behavior_for
from fairdrop_sim.challenge.captcha import CaptchaModel
from fairdrop_sim.challenge.solver import Solver
from fairdrop_sim.crowd.population import build_bots
from fairdrop_sim.models.scenario import AttackProfile, AttackerCost


def test_all_nine_profiles_present():
    expected = set(AttackProfile.__args__)  # type: ignore[attr-defined]
    assert set(PROFILES) == expected and set(SUGGESTED) == expected


def test_build_bots_ip_allocation():
    distributed = build_bots(0, 1000, 200, master_seed=42)
    assert len(set(distributed.user_ids)) == 1000
    assert len(set(distributed.client_ips)) == 200  # M distinct controlled IPs
    sybil = build_bots(1, 1000, 1, master_seed=42)
    assert len(set(sybil.client_ips)) == 1  # one IP for the whole farm
    assert build_bots(0, 1000, 200, 42).user_ids == distributed.user_ids  # deterministic
    assert build_bots(1, 10, 1, 42).user_ids != build_bots(2, 10, 1, 42).user_ids  # per-attacker namespace


def test_shared_device_groups_by_ip():
    b = build_bots(0, 100, 10, 42, shared_device=True)
    assert len(set(b.device_ids)) == 10  # one device per IP
    assert len(set(build_bots(0, 100, 10, 42, shared_device=False).device_ids)) == 100


def test_behavior_override():
    base = PROFILES["naive_flooder"]
    assert base.solves_pow is False
    assert behavior_for("naive_flooder", {"solves_pow": True}).solves_pow is True
    assert behavior_for("naive_flooder", {"solves_pow": None}).solves_pow is False
    assert behavior_for("human_mimic").solves_pow is True  # natural default kept


def test_cost_account_merge_and_usd():
    a, b = CostAccount(), CostAccount()
    a.note_request("u1", "1.1.1.1")
    a.add_pow_hashes(1_000_000)
    b.note_request("u2", "1.1.1.2")
    b.add_captcha_solves(3)
    b.note_request("u1", "1.1.1.1")  # same identity again
    m = CostAccount.merge([a.to_dict(), b.to_dict()])
    assert m.requests == 3 and len(m.accounts) == 2 and len(m.ips) == 2
    assert m.pow_hashes == 1_000_000 and m.captcha_solves == 3
    cost = AttackerCost(per_account=0.1, per_ip=0.5, per_captcha=0.002, per_1e9_hashes=1.0, per_1e6_requests=0.05)
    # 2*0.1 + 2*0.5 + 3*0.002 + 1e6/1e9*1 + 3/1e6*0.05
    assert m.usd(cost) == pytest.approx(0.2 + 1.0 + 0.006 + 0.001 + 0.00000015, abs=1e-6)
    summ = m.summary(cost, seats_won=2)
    assert summ["per_seat"]["accounts"] == 1.0 and summ["seats_won"] == 2
    assert m.summary(cost, 0)["per_seat"]["requests"] is None  # unbounded when no seats


def test_solver_pow_real_counts_hashes():
    cost = CostAccount()
    solver = Solver(hash_rate=1e9, pow_mode="modelled_delay", sink=cost)
    ch = {"id": "ch1", "type": "pow", "pow": {"algo": "sha256-lzb", "prefix": "fd1.e.u.7", "difficulty_bits": 12}}
    from mock_server.defences import verify_pow

    cid, nonce = asyncio.run(solver.solve(ch, random.Random(0)))
    assert cid == "ch1" and verify_pow("fd1.e.u.7", nonce, 12)
    assert cost.pow_hashes == int(nonce) + 1  # every attempt counted


def test_solver_pow_too_hard_gives_up():
    cost = CostAccount()
    solver = Solver(max_pow_bits=20, sink=cost)
    ch = {"id": "c", "type": "pow", "pow": {"prefix": "p", "difficulty_bits": 40}}
    assert asyncio.run(solver.solve(ch, random.Random(0))) is None
    assert "pow_too_hard:40" in cost.give_ups


def test_solver_captcha_model():
    cost = CostAccount()
    always = Solver(captcha=CaptchaModel(solve_s_mean=0.0, fail_rate=0.0), sink=cost)
    cid, tok = asyncio.run(always.solve({"id": "c", "type": "captcha"}, random.Random(0)))
    assert tok == "mock-captcha-ok" and cost.captcha_solves == 1
    never = Solver(captcha=CaptchaModel(solve_s_mean=0.0, fail_rate=1.0), sink=CostAccount())
    assert asyncio.run(never.solve({"id": "c", "type": "captcha"}, random.Random(0))) is None
