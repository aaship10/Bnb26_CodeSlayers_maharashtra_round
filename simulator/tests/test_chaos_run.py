"""Chaos orchestration: outage metrics on known shapes, command hooks, and a real orchestrated run
against the in-process mock (the fault command is harmless; what is tested is timing, bookkeeping
and the verdict)."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.engine.run import TargetConfig
from fairdrop_sim.models import Scenario
from fairdrop_sim.runner.chaos_run import ChaosStep, chaos_metrics, command_hook, failure_timeline, run_chaos
from tests.test_engine_e2e import mock_url, scenario  # noqa: F401

PY = sys.executable


# --------------------------------------------------------------------------- the metric, on known shapes
# timeline: tenth-of-a-second -> (ok, failed)


def test_total_outage_is_the_stretch_with_no_success_even_through_silence():
    tl = {t: (9, 0) for t in range(0, 30)}
    tl.update({30: (0, 12), 31: (0, 3), 45: (0, 2), 60: (0, 1)})  # failures with silent gaps: clients backing off
    tl.update({t: (8, 0) for t in range(80, 120)})
    m = chaos_metrics(tl, fault_at_s=3.0, heal_done_s=7.5)
    assert m["outage_seconds"] == 5.0 and m["recovered"] is True  # 3.0 s -> first success at 8.0 s
    assert m["requests_failed"] == 18 and m["first_failure_s"] == 3.0 and m["last_failure_s"] == 6.0
    assert m["recovery_seconds"] == 0.0  # failures stopped before the heal finished


def test_a_partial_failure_is_not_an_outage():
    m = chaos_metrics({t: (6, 1) for t in range(0, 100)}, 3.0, 6.0)  # successes between every failure
    assert m["outage_seconds"] == 0.0 and m["requests_failed"] == 100 and m["degraded_seconds"] == 7


def test_a_clean_run_reports_nothing_failed():
    m = chaos_metrics({t: (9, 0) for t in range(0, 50)}, 2.0, 4.0)
    assert m["requests_failed"] == 0 and m["outage_seconds"] == 0.0 and m["recovery_seconds"] is None
    assert m["first_failure_s"] is None


def test_never_recovering_is_flagged():
    tl = {t: (9, 0) for t in range(0, 30)}
    tl.update({t: (0, 4) for t in range(30, 60)})
    m = chaos_metrics(tl, 3.0, 5.0)
    assert m["recovered"] is False and m["outage_seconds"] == 3.0  # measured to the end of the data


def test_failures_before_the_fault_do_not_count_as_its_outage():
    tl = {t: (9, 0) for t in range(0, 100)}
    tl.update({5: (0, 4), 6: (0, 4)})  # an unrelated blip at 0.5 s, long before the fault at 5 s
    assert chaos_metrics(tl, 5.0, 8.0)["outage_seconds"] == 0.0
    assert chaos_metrics(tl, 0.0, 8.0)["outage_seconds"] == 0.2


def test_timeline_is_built_from_completion_time_not_intended_time():
    """The bug the first real chaos run exposed: a request scheduled at second 2 that fails at second 5
    belongs to second 5."""
    rec = Recorder(0.0)
    rec.request("enter", "legit", 200, None, intended=2.0, t_send=2.0, t_recv=2.05)
    rec.request("enter", "legit", 0, "CONN_ERROR", intended=2.0, t_send=2.0, t_recv=5.2)
    tl = failure_timeline(rec)
    assert tl[20] == (1, 0) and tl[52] == (0, 1)
    assert "2|enter.legit|CONN_ERROR" in rec.timeline  # the by-intended-time view still exists


def test_recorder_merge_keeps_completions_and_tolerates_old_files():
    a, b = Recorder(0.0), Recorder(0.0)
    a.request("enter", "legit", 200, None, 0.1, 0.1, 0.2)
    b.request("enter", "legit", 500, None, 0.1, 0.1, 0.25)
    merged = Recorder.merged([a.to_dict(), b.to_dict()], 0.0)
    assert merged.completions["2|ok"] == 1 and merged.completions["2|fail"] == 1
    old = a.to_dict()
    del old["completions"]  # a recorder.json written before this field existed
    assert Recorder.merged([old], 0.0).completions == {}


# --------------------------------------------------------------------------- command hooks


@pytest.mark.asyncio
async def test_command_hook_returns_output_and_fails_loudly_on_nonzero_exit():
    ok = await command_hook(ChaosStep("say", 0, [PY, "-c", "print('injected')"])).fn()
    assert ok["exit_code"] == 0 and "injected" in ok["stdout"]
    with pytest.raises(RuntimeError, match="boom exited 3"):
        await command_hook(ChaosStep("boom", 0, [PY, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"])).fn()
    with pytest.raises(Exception):  # a command that does not exist is reported, not ignored
        await command_hook(ChaosStep("ghost", 0, ["definitely-not-a-command-xyz"])).fn()


# --------------------------------------------------------------------------- orchestrated runs (mock)


def small(**over) -> Scenario:
    return scenario(**over)


@pytest.mark.asyncio
async def test_hooks_fire_at_their_offsets_during_the_load(mock_url, tmp_path):  # noqa: F811
    steps = [ChaosStep("inject", 1.0, [PY, "-c", "print('x')"], role="inject"),
             ChaosStep("heal", 2.0, [PY, "-c", "print('y')"], role="heal")]
    res = await run_chaos(small(), steps, TargetConfig(base_url=mock_url), tmp_path, log=lambda _m: None)
    c = res.chaos
    assert res.passed and c["steps_ok"] and c["invariants_passed"] and c["ordering_ok"]
    by = {s["name"]: s for s in c["steps"]}
    assert 0.9 <= by["inject"]["started_at_s"] <= 1.6, by  # fired about 1 s after the window opened
    assert 1.9 <= by["heal"]["started_at_s"] <= 2.6, by
    assert c["requests_failed"] == 0 and c["outage_seconds"] == 0.0 and c["recovered"] is True
    assert (tmp_path / res.summary["run_id"] / "chaos.json").exists()
    assert json.loads((tmp_path / res.summary["run_id"] / "chaos.json").read_text())["fault_at_s"] == 1.0


@pytest.mark.asyncio
async def test_a_failing_injection_fails_the_run_visibly(mock_url, tmp_path):  # noqa: F811
    steps = [ChaosStep("inject", 0.5, [PY, "-c", "import sys; sys.exit(7)"], role="inject")]
    res = await run_chaos(small(), steps, TargetConfig(base_url=mock_url), tmp_path, log=lambda _m: None)
    assert not res.passed and res.chaos["steps_ok"] is False
    assert any("inject" in w and "did not run cleanly" in w for w in res.summary["warnings"])


@pytest.mark.asyncio
async def test_a_hook_due_after_the_run_ends_is_reported_not_dropped(mock_url, tmp_path):  # noqa: F811
    steps = [ChaosStep("late", 600.0, [PY, "-c", "print(1)"])]
    res = await run_chaos(small(), steps, TargetConfig(base_url=mock_url), tmp_path, log=lambda _m: None)
    late = next(s for s in res.chaos["steps"] if s["name"] == "late")
    assert late["ok"] is False and "before this hook was due" in late["error"] and not res.passed


@pytest.mark.asyncio
async def test_heal_that_starts_before_the_inject_finished_is_a_race_and_fails_the_run(mock_url, tmp_path):  # noqa: F811
    """The exact mistake made in the first real chaos run: an injection slower than the gap to the heal."""
    steps = [ChaosStep("slow-kill", 0.5, [PY, "-c", "import time; time.sleep(1.5)"], role="inject"),
             ChaosStep("heal", 1.0, [PY, "-c", "print(1)"], role="heal")]
    res = await run_chaos(small(), steps, TargetConfig(base_url=mock_url), tmp_path, log=lambda _m: None)
    assert res.chaos["ordering_ok"] is False and not res.passed


@pytest.mark.asyncio
async def test_control_calls_ride_out_an_outage_but_not_a_refusal(mock_url, monkeypatch):  # noqa: F811
    import httpx

    from fairdrop_sim.adapters.api_adapter import AdminError
    from fairdrop_sim.engine import run as runmod

    monkeypatch.setattr(runmod, "CONTROL_RETRY_S", 3.0)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("down")
        return "closed"

    info: dict = {}
    assert await runmod._retry(flaky, "close", lambda _m: None, info) == "closed"
    assert calls["n"] == 3 and info["control_retries"] == 2

    async def refused():
        class R:  # an HTTP 409 from the engine: a real answer, so no retry
            status_code, text = 409, "{}"
            def json(self): return {"code": "INVALID_PHASE"}
        raise AdminError("close", R())

    n_before = info.get("control_retries", 0)
    with pytest.raises(AdminError):
        await runmod._retry(refused, "close", lambda _m: None, info)
    assert info.get("control_retries", 0) == n_before  # not retried

    async def never():
        raise httpx.ConnectError("still down")

    with pytest.raises(httpx.ConnectError):  # bounded: gives up after CONTROL_RETRY_S
        await runmod._retry(never, "close", lambda _m: None, {})
