"""End-to-end metrics aggregation: fabricate run directories with KNOWN composition,
aggregate, and check the Results numbers and schema."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.metrics.aggregate import build_results
from fairdrop_sim.models.results import Results
from tests.test_metrics import population


def _recorder() -> dict:
    rec = Recorder(0.0)
    for i in range(300):
        rec.request("enter", "legit", 200, None, i * 0.01, i * 0.01, i * 0.01 + 0.02)
    for i in range(50):
        rec.request("enter", "bot", 200, None, i * 0.01, i * 0.01, i * 0.01 + 0.05)
    for i in range(100):
        rec.request("status", "legit", 200, None, i * 0.01, i * 0.01, i * 0.01 + 0.01)
    for i in range(20):
        rec.request("claim", "legit", 200, None, i * 0.01, i * 0.01, i * 0.01 + 0.015)
    rec.request("enter", "legit", 429, "RATE_LIMITED", 0.5, 0.5, 0.51)
    return rec.to_dict()


def write_run(d: Path, *, seed: int, bot_seats: int, bot_entrants: int, human_entrants: int,
              human_seats: int, cost_requests: float, passed: bool = True,
              availability: float = 0.99) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    users = population(n_human_seats=human_seats, n_bot_seats=bot_seats,
                       n_human_entrants=human_entrants, n_bot_entrants=bot_entrants)
    users["idx"] = range(len(users))
    users["profile"] = ["legit" if l == "legit" else "sybil_single_ip" for l in users["label"]]
    with gzip.open(d / "users.csv.gz", "wt", encoding="utf-8", newline="") as f:
        users.to_csv(f, index=False)
    (d / "recorder.json").write_text(json.dumps(_recorder()), encoding="utf-8")
    (d / "decisions.json").write_text("[]", encoding="utf-8")
    meta = {
        "run_seed": seed, "target": "mock", "synthetic": False,
        "population": {"legit": human_entrants, "bots": 1, "bot_identities": bot_entrants},
        "scenario_config": {"seed": 123, "name": "unit_agg",
                            "event": {"inventory": human_seats + bot_seats, "mode": "LOTTERY",
                                      "defences": {"preset": "none"}}},
        "attackers": [{"profile": "sybil_single_ip", "seats_won": bot_seats,
                       "cost": {"totals": {"requests": cost_requests, "accounts": bot_entrants,
                                           "pow_hashes": 0, "captcha_solves": 0, "usd_modelled": bot_entrants * 0.1}}}],
        "load": {"availability_spike": availability},
        "integrity": {"oversold": 0, "duplicate_users": 0, "duplicate_seats": 0, "orphaned_holds": 0,
                      "passed": passed, "draw_verified": True},
    }
    (d / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def test_aggregate_known_shares(tmp_path):
    dirs = []
    for i, bs in enumerate((18, 20, 22)):  # bot seats per run; mean 20 of 100
        dirs.append(write_run(tmp_path / f"r{i}", seed=1000 + i, bot_seats=bs, bot_entrants=200,
                              human_entrants=800, human_seats=100 - bs, cost_requests=1000 + i * 1000))
    res = build_results(dirs, run_id="agg-test", boot_resamples=2000, perm_resamples=500)
    assert isinstance(res, Results) and res.schema_version == 1 and res.repeats == 3

    f = res.metrics.fairness
    assert f.bot_seat_share.mean == pytest.approx(0.20, abs=1e-9)
    assert f.bot_seat_share.ci_low <= 0.20 <= f.bot_seat_share.ci_high and f.bot_seat_share.n == 300  # pooled Wilson: total seats
    assert f.bot_entrant_share.mean == pytest.approx(0.20)
    assert f.human_win_prob.mean == pytest.approx((82 + 80 + 78) / 3 / 800)
    # cost per seat: Σrequests / Σbot seats = (1000+2000+3000)/(18+20+22) = 6000/60 = 100
    assert f.attacker_cost_per_seat.requests.mean == pytest.approx(100.0)
    assert f.attacker_cost_per_seat.accounts.mean == pytest.approx(600 / 60)

    # latency pooled across runs; enter had 350 reqs/run (+1 rate-limited) => 3*351
    assert res.metrics.system.latency_ms["enter"].n == 3 * 351
    assert "enter.bot" in res.metrics.system.latency_ms
    assert res.metrics.system.error_rates.http_429_legit.mean > 0  # the injected 429s

    assert res.metrics.integrity.passed is True
    assert res.metrics.detection.precision is None  # no defences in these runs

    # survives its own schema and a JSON round-trip (what D will parse)
    assert Results.model_validate_json(res.model_dump_json()) == res


def test_one_failed_run_turns_result_red(tmp_path):
    good = write_run(tmp_path / "g", seed=1, bot_seats=20, bot_entrants=100, human_entrants=100, human_seats=80,
                     cost_requests=500)
    bad = write_run(tmp_path / "b", seed=2, bot_seats=20, bot_entrants=100, human_entrants=100, human_seats=80,
                    cost_requests=500, passed=False)
    res = build_results([good, bad], boot_resamples=500, perm_resamples=200)
    assert res.metrics.integrity.passed is False
    assert res.failed_runs == 1
    assert sum(1 for p in res.per_run if p.status == "failed") == 1


# --------------------------------------------------------------------------- degenerate cells


def write_lockout_run(d: Path, *, seed: int, humans: int = 200, bots: int = 50) -> Path:
    """Every identity locked out: intended to enter, nobody did, no seats."""
    from tests.test_metrics import make_users

    d.mkdir(parents=True, exist_ok=True)
    rows = [{"user_id": f"h{i}", "label": "legit", "nat_group": -1} for i in range(humans)]
    rows += [{"user_id": f"b{i}", "label": "bot", "nat_group": -1} for i in range(bots)]
    users = make_users(rows)
    users["idx"] = range(len(users))
    users["profile"] = ["legit" if l == "legit" else "sybil_single_ip" for l in users["label"]]
    with gzip.open(d / "users.csv.gz", "wt", encoding="utf-8", newline="") as f:
        users.to_csv(f, index=False)
    (d / "recorder.json").write_text(json.dumps(_recorder()), encoding="utf-8")
    (d / "decisions.json").write_text("[]", encoding="utf-8")
    meta = {
        "run_seed": seed, "target": "mock", "synthetic": False,
        "population": {"legit": humans, "bots": 1, "bot_identities": bots},
        "scenario_config": {"seed": 123, "name": "lockout", "event": {"inventory": 20, "mode": "LOTTERY",
                                                                    "defences": {"preset": "all"}}},
        "attackers": [{"profile": "sybil_single_ip", "seats_won": 0,
                       "cost": {"totals": {"requests": 100, "accounts": bots, "pow_hashes": 0,
                                           "captcha_solves": 0, "usd_modelled": 1.0}}}],
        "load": {"availability_spike": 1.0},
        "integrity": {"oversold": 0, "duplicate_users": 0, "duplicate_seats": 0, "orphaned_holds": 0,
                      "passed": True, "draw_verified": True},
    }
    (d / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def test_total_lockout_does_not_crash_and_is_null_not_zero(tmp_path):
    """A defence that locks everyone out leaves 0/0 shares. They must come back null with an
    explanation (never an invented 0%), while human metrics are real zeros."""
    dirs = [write_lockout_run(tmp_path / f"l{i}", seed=i) for i in range(2)]
    res = build_results(dirs, boot_resamples=500, perm_resamples=200)
    f = res.metrics.fairness
    assert f.bot_seat_share is None and f.bot_entrant_share is None
    assert f.human_entry_success_rate.mean == 0.0 and f.human_entry_success_rate.n == 400
    assert f.human_win_prob.mean == 0.0
    assert f.arrival_time_correlation is None and f.jain_index is None  # undefined, no entrants/winners
    assert any("bot_seat_share is null" in n for n in res.notes)
    assert any("bot_entrant_share is null" in n for n in res.notes)
    assert res.metrics.integrity.passed is True
    assert Results.model_validate_json(res.model_dump_json()) == res  # still schema-valid


def test_partial_lockout_pools_counts_across_runs(tmp_path):
    """One run fully locked out + one normal run: shares pool over the runs that had data."""
    ok = write_run(tmp_path / "ok", seed=1, bot_seats=20, bot_entrants=200, human_entrants=800, human_seats=80,
                   cost_requests=1000)
    locked = write_lockout_run(tmp_path / "lock", seed=2, humans=800, bots=200)
    res = build_results([ok, locked], boot_resamples=500, perm_resamples=200)
    f = res.metrics.fairness
    assert f.bot_seat_share.mean == pytest.approx(0.20) and f.bot_seat_share.n == 100  # only run 1 seated anyone
    assert f.human_entry_success_rate.mean == pytest.approx(800 / 1600)  # run 2 contributed 0 of 800
    assert not any("is null" in n for n in res.notes)


def test_no_simulated_humans_raises_clear_error(tmp_path):
    """No legit users at all is a scenario mistake, not a defence effect: say so."""
    from tests.test_metrics import make_users

    d = tmp_path / "nohumans"
    d.mkdir()
    users = make_users([{"user_id": f"b{i}", "label": "bot", "nat_group": -1} for i in range(10)])
    users["idx"] = range(len(users))
    users["profile"] = "sybil_single_ip"
    with gzip.open(d / "users.csv.gz", "wt", encoding="utf-8", newline="") as f:
        users.to_csv(f, index=False)
    (d / "recorder.json").write_text(json.dumps(_recorder()), encoding="utf-8")
    (d / "decisions.json").write_text("[]", encoding="utf-8")
    (d / "run.json").write_text(json.dumps({
        "run_seed": 1, "target": "mock", "synthetic": False,
        "population": {"legit": 0, "bots": 1, "bot_identities": 10},
        "scenario_config": {"seed": 1, "name": "nohumans", "event": {"inventory": 5, "mode": "LOTTERY"}},
        "attackers": [], "load": {}, "integrity": {"passed": True, "draw_verified": True},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="no legit users"):
        build_results([d], boot_resamples=200, perm_resamples=100)
