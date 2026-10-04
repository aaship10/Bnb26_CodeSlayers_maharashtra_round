"""End-to-end: a real (small) open-loop run against the mock server over HTTP."""
from __future__ import annotations

import asyncio
import gzip
import socket
import threading
import time

import pandas as pd
import pytest
import uvicorn

from fairdrop_sim.engine.run import TargetConfig, TargetMismatch, execute_run
from fairdrop_sim.models import Scenario
from mock_server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_url():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error",
                                           timeout_keep_alive=75))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "mock did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


def scenario(**over) -> Scenario:
    base = {
        "name": "e2e_small", "target": "mock", "seed": 99, "repeats": 2,
        "event": {"inventory": 20, "window_seconds": 2, "claim_ttl_seconds": 2, "mode": "LOTTERY"},
        "legit": {"count": 300, "poll": {"interval_s_mean": 0.5, "max_polls": 10},
                  "claim": {"delay_lognormal_mu": -1.5, "delay_lognormal_sigma": 0.3, "no_show_rate": 0.0},
                  "retry": {"jitter_ms_mean": 200}},
        "load": {"max_in_flight": 200, "procs": 1, "lead_s": 1.5, "claim_phase_s": 4},
    }
    for k, v in over.items():
        base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) else v
    return Scenario.model_validate(base)


def _run(sc: Scenario, url: str, tmp_path, idx: int = 0) -> dict:
    return asyncio.run(execute_run(sc, idx, TargetConfig(base_url=url), out_dir=tmp_path, log=lambda _m: None))


def test_lottery_run_end_to_end(mock_url, tmp_path):
    s = _run(scenario(), mock_url, tmp_path)
    o = s["outcomes"]
    assert s["target"] == "mock" and s["integrity"]["passed"] and s["integrity"]["draw_verified"] is True
    assert o["human_entry_success_rate"] == 1.0 and o["entered_server"] == 300
    assert o["draw_states"] == {"WON": 20, "WAITLISTED": 280}
    assert o["final_states"].get("CLAIMED") == 20  # no no-shows, fast claims
    assert s["endpoints"]["enter"]["open_loop_ms"]["n"] >= 300
    assert s["endpoints"]["claim"]["ok"] == 20
    assert s["load"]["scheduler_lag_ms"]["p50"] < 20
    users = pd.read_csv(gzip.open(f"{s['artifacts']}/users.csv.gz", "rt"))
    assert len(users) == 300 and set(users["label"]) == {"legit"}
    assert users["srv_arrival_seq"].notna().all()


def test_fcfs_run_and_same_seed_same_population(mock_url, tmp_path):
    s = _run(scenario(event={"mode": "FCFS"}), mock_url, tmp_path)
    assert s["integrity"]["passed"] and s["integrity"]["draw_verified"] is None
    assert s["outcomes"]["final_states"].get("CLAIMED") == 20
    users = pd.read_csv(gzip.open(f"{s['artifacts']}/users.csv.gz", "rt"))
    winners = users[users["srv_draw_state"] == "WON"]
    # FCFS: holds went to the earliest server arrivals
    assert winners["srv_arrival_seq"].max() <= 20


def test_two_process_shards(mock_url, tmp_path):
    s = _run(scenario(load={"procs": 2}), mock_url, tmp_path, idx=1)
    assert s["integrity"]["passed"]
    assert s["outcomes"]["entered_server"] == 300 and s["outcomes"]["human_entry_success_rate"] == 1.0


def test_target_mismatch_is_refused(mock_url, tmp_path):
    with pytest.raises(TargetMismatch):
        _run(scenario(target="real"), mock_url, tmp_path)


# --------------------------------------------------------------------------- bots (Stage 3)


def _attacker_by_profile(summary: dict, profile: str) -> dict:
    return next(a for a in summary["attackers"] if a["profile"] == profile)


def test_lottery_speed_buys_nothing(mock_url, tmp_path):
    """Same identities entering => same draw, whatever the request volume. The lottery
    outcome is bit-identical at 1x and 10x request rate: speed and volume buy no seats."""
    base = dict(event={"inventory": 40, "mode": "LOTTERY", "window_seconds": 2},
                legit={"count": 200}, load={"lead_s": 1.5, "claim_phase_s": 3})
    slow = scenario(**base, attackers=[{"profile": "sybil_single_ip", "identities": 100, "ips": 1,
                                        "rps_per_identity": 1}])
    fast = scenario(**base, attackers=[{"profile": "sybil_single_ip", "identities": 100, "ips": 1,
                                        "rps_per_identity": 1, "request_multiplier": 10}])
    s1 = _run(slow, mock_url, tmp_path, idx=0)
    s2 = _run(fast, mock_url, tmp_path, idx=0)
    assert s1["fairness"]["bot_entries"] == s2["fairness"]["bot_entries"] == 100
    # bots are 100/300 of entrants; the lottery gives them about that, nowhere near all,
    # and 10x the request volume does not move the share (the draw ignores request count).
    # (The draw's exact order-independence is covered by test_draw_* in test_mock_server.)
    for share in (s1["fairness"]["bot_seat_share"], s2["fairness"]["bot_seat_share"]):
        assert 0.12 <= share <= 0.55
    assert abs(s1["fairness"]["bot_seat_share"] - s2["fairness"]["bot_seat_share"]) <= 0.15
    assert s1["integrity"]["passed"] and s2["integrity"]["passed"]


def test_fcfs_speed_wins(mock_url, tmp_path):
    """Under FCFS, bots that fire at t=0 grab a share far above their entrant share."""
    sc = scenario(event={"inventory": 20, "mode": "FCFS", "window_seconds": 2},
                  legit={"count": 300, "arrival": {"kind": "uniform"}},
                  load={"lead_s": 1.5, "claim_phase_s": 3},
                  attackers=[{"profile": "speed_bot", "identities": 30, "ips": 30, "rps_per_identity": 20}])
    s = _run(sc, mock_url, tmp_path)
    f = s["fairness"]
    assert f["bot_entrant_share"] < 0.12  # 30 of ~330 entrants
    assert f["bot_seat_share"] >= 0.4  # but they take a big share of the 20 seats
    assert f["bot_seat_share"] > 3 * f["bot_entrant_share"]
    assert s["integrity"]["passed"]


def test_pow_defence_mimic_solves_sybil_gives_up(mock_url, tmp_path):
    """With PoW on enter: humans and the PoW-solving mimic get in; a Sybil farm that
    does not solve is locked out. Mimic pays real hashes; the cost is counted."""
    sc = scenario(
        event={"inventory": 40, "mode": "LOTTERY", "window_seconds": 3,
               "defences": {"preset": "custom", "layers": {"pow": {"enabled": True, "difficulty_bits": 8}}}},
        legit={"count": 100, "device": {"pow_mode": "real"}},
        load={"lead_s": 1.5, "claim_phase_s": 3},
        attackers=[
            {"profile": "human_mimic", "identities": 50, "ips": 50, "rps_per_identity": 1,
             "solves_pow": True, "solves_captcha": False, "pow_mode": "real", "hash_rate": 1e7},
            {"profile": "sybil_single_ip", "identities": 50, "ips": 1, "rps_per_identity": 1},
        ])
    s = _run(sc, mock_url, tmp_path)
    mimic = _attacker_by_profile(s, "human_mimic")
    sybil = _attacker_by_profile(s, "sybil_single_ip")
    assert mimic["challenges_seen"] > 0
    assert mimic["server_entries"] > 0 and mimic["cost"]["totals"]["pow_hashes"] > 0
    assert sybil["server_entries"] == 0  # never solved the PoW, never entered
    assert s["outcomes"]["entered_server"] > 0  # humans solved and entered
    assert s["integrity"]["passed"]


def test_distributed_botnet_presents_many_ips(mock_url, tmp_path):
    """distributed_botnet must reach the server from M distinct IPs (via X-Sim-Client-IP)."""
    import gzip

    import pandas as pd

    sc = scenario(event={"inventory": 30, "mode": "LOTTERY", "window_seconds": 2},
                  legit={"count": 100}, load={"lead_s": 1.5, "claim_phase_s": 2},
                  attackers=[{"profile": "distributed_botnet", "identities": 200, "ips": 50,
                              "rps_per_identity": 1}])
    s = _run(sc, mock_url, tmp_path)
    users = pd.read_csv(gzip.open(f"{s['artifacts']}/users.csv.gz", "rt"))
    bots = users[users["label"] == "bot"]
    assert len(bots) == 200 and bots["client_ip"].nunique() == 50
    # the server saw those same controlled IPs, not the one loopback address
    assert bots["srv_client_ip"].dropna().nunique() == 50
