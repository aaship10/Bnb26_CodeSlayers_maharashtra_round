"""The /sim HTTP service: contract against D's zod schemas, SSE resume, errors, cancel,
persistence, charts and experiments. One real (small) run goes through the whole stack:
HTTP -> run manager -> open-loop engine -> mock server -> metrics -> results."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fairdrop_sim.service.app import create_app
from tests.test_engine_e2e import mock_url  # noqa: F401  (module-scoped in-process mock fixture)

REPO = Path(__file__).resolve().parents[2]
CHECK = Path(__file__).parent / "contract" / "service_payloads.check.ts"
TSX = REPO / "frontend" / "node_modules" / ".bin" / ("tsx.cmd" if sys.platform == "win32" else "tsx")
SMALL = {"scenario_id": "flash_crowd", "overrides": {"legit_users": 100, "inventory": 10}, "repeats": 1,
         "seed": 7, "target": "mock"}


def wait_for(client: TestClient, run_id: str, states: set[str], timeout: float = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = client.get(f"/sim/runs/{run_id}").json()
        if v["status"] in states:
            return v
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} never reached {states}: {v}")


def parse_sse(text: str) -> list[dict]:
    out, cur = [], {}
    for line in text.splitlines():
        if line.startswith("id:"):
            cur["id"] = int(line[3:].strip())
        elif line.startswith("event:"):
            cur["event"] = line[6:].strip()
        elif line.startswith("data:"):
            cur["data"] = json.loads(line[5:])
        elif line == "" and cur:
            out.append(cur)
            cur = {}
    return out


@pytest.fixture(scope="module")
def svc(mock_url, tmp_path_factory):  # noqa: F811
    results_dir = tmp_path_factory.mktemp("svc_results")
    mp = pytest.MonkeyPatch()
    mp.setenv("FD_MOCK_URL", mock_url)
    mp.setenv("FD_REAL_URL", "http://127.0.0.1:9")  # nothing listens here: the "real stack is down" case
    app = create_app(results_dir=results_dir, include_samples=True, heartbeat_s=0.5)
    with TestClient(app) as client:
        client.results_dir = results_dir  # type: ignore[attr-defined]
        yield client
    mp.undo()


@pytest.fixture(scope="module")
def done_run(svc):
    """One real run through the whole stack (about 20 s), shared by the tests below."""
    r = svc.post("/sim/runs", json=SMALL)
    assert r.status_code == 201, r.text
    rid = r.json()["run_id"]
    wait_for(svc, rid, {"done", "failed", "cancelled"})
    return rid


# --------------------------------------------------------------------------- a real run


def test_real_run_completes_with_valid_results(svc, done_run):
    v = svc.get(f"/sim/runs/{done_run}").json()
    assert v["status"] == "done" and v["progress"] == 1.0 and v["target"] == "mock"
    res = svc.get(f"/sim/runs/{done_run}/results").json()
    assert res["schema_version"] == 1 and res["target"] == "mock" and res["synthetic"] is False
    assert res["metrics"]["integrity"]["passed"] is True and res["metrics"]["integrity"]["draw_verified"] is True
    assert res["population"] == {"legit": 100, "bots": 0, "bot_identities": 0}
    f = res["metrics"]["fairness"]
    assert f["human_entry_success_rate"]["mean"] > 0.9 and f["human_entry_success_rate"]["n"] == 100
    assert res["event"]["inventory"] == 10 and res["event"]["mode"] == "LOTTERY"


def test_listed_run_summary_has_everything_d_needs(svc, done_run):
    row = next(r for r in svc.get("/sim/runs").json() if r["run_id"] == done_run)
    assert row["scenario_name"] == "Flash crowd, no attack" and row["repeats"] == 1 and row["seed"] == 7
    assert row["params"]["legit_users"] == 100 and row["params"]["mode"] == "LOTTERY"
    assert row["created_at"] and row["finished_at"]


def replay(svc, run_id: str, last_event_id: int = 0) -> list[dict]:
    with svc.stream("GET", f"/sim/runs/{run_id}/stream", headers={"Last-Event-ID": str(last_event_id)}) as r:
        return parse_sse("".join(r.iter_text()))


def test_fresh_connection_gets_the_current_status_then_ends_when_terminal(svc, done_run):
    """No Last-Event-ID: the page is never blank (current status first); a finished run ends the stream."""
    with svc.stream("GET", f"/sim/runs/{done_run}/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert body.startswith("retry: 2000")
    events = parse_sse(body)
    assert len(events) == 1 and events[0]["event"] == "status" and events[0]["data"]["status"] == "done"


def test_sse_history_is_ordered_complete_and_resumable(svc, done_run):
    events = replay(svc, done_run, 0)  # Last-Event-ID 0 = everything since the start
    ids = [e["id"] for e in events]
    assert ids == list(range(1, len(ids) + 1))  # contiguous, in order, nothing skipped
    assert {"status", "progress", "snapshot"} <= {e["event"] for e in events}
    assert events[0]["event"] == "status" and events[0]["data"]["status"] == "queued"
    assert events[-1]["event"] == "status" and events[-1]["data"]["status"] == "done"
    progress = [e["data"]["progress"] for e in events if e["event"] == "progress"]
    assert progress == sorted(progress) and 0 <= progress[0] and progress[-1] < 1.0  # monotonic, never early-complete
    # a snapshot with real measured values arrives when the repeat finishes
    final_snap = [e for e in events if e["event"] == "snapshot" and "throughput_rps" in e["data"]][-1]["data"]
    assert final_snap["repeat"] == 1 and final_snap["requests"] > 100 and final_snap["p95_ms"] > 0
    # resume from the middle: exactly the events after that id
    mid = ids[len(ids) // 2]
    assert [e["id"] for e in replay(svc, done_run, mid)] == [i for i in ids if i > mid]
    assert replay(svc, done_run, ids[-1]) == []  # fully caught up: nothing to send, and it still terminates


def test_live_stream_delivers_events_while_the_run_is_in_flight(svc, done_run):
    """Open the stream while a run is running (no Last-Event-ID) and read until it ends."""
    rid = svc.post("/sim/runs", json=SMALL).json()["run_id"]
    wait_for(svc, rid, {"running"}, timeout=30)
    with svc.stream("GET", f"/sim/runs/{rid}/stream") as r:
        events = parse_sse("".join(r.iter_text()))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "status" and "progress" in kinds and "snapshot" in kinds
    assert events[-1]["data"]["status"] == "done"


def test_service_payloads_pass_d_zod_schemas(svc, done_run, tmp_path):
    if not TSX.exists() or shutil.which("node") is None:
        pytest.skip("frontend node_modules not installed")
    snap = [e for e in replay(svc, done_run, 0) if e["event"] == "snapshot"][-1]["data"]
    files = {
        "scenarios.json": svc.get("/sim/scenarios").json(),
        "run.json": svc.get(f"/sim/runs/{done_run}").json(),
        "runs.json": svc.get("/sim/runs").json(),
        "results.json": svc.get(f"/sim/runs/{done_run}/results").json(),
        "experiments.json": svc.get("/sim/experiments").json(),
        "chart.json": svc.get("/sim/charts/bot_share_vs_identities?experiment=synthetic-E2").json(),
        "snapshot.json": snap,
    }
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    r = subprocess.run([str(TSX), str(CHECK), str(tmp_path)], capture_output=True, text=True, cwd=REPO / "frontend",
                       timeout=120, shell=sys.platform == "win32")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("OK ") == 7


def test_restart_keeps_finished_runs_and_fails_interrupted_ones(svc, done_run):
    d = svc.results_dir / "service"
    ghost = d / "run_ghost"
    ghost.mkdir()
    (ghost / "meta.json").write_text(json.dumps({
        "run_id": "run_ghost", "scenario_id": "flash_crowd", "status": "running", "progress": 0.4,
        "scenario_name": "x", "params": {}, "repeats": 1, "seed": 1, "target": "mock"}), encoding="utf-8")
    with TestClient(create_app(results_dir=svc.results_dir, include_samples=False)) as fresh:
        assert fresh.get(f"/sim/runs/{done_run}").json()["status"] == "done"
        assert fresh.get(f"/sim/runs/{done_run}/results").json()["run_id"] == done_run
        g = fresh.get("/sim/runs/run_ghost").json()
        assert g["status"] == "failed" and g["error"] == "service restarted"
        assert fresh.get("/sim/experiments").json() == []  # samples switched off


# --------------------------------------------------------------------------- contract & errors


def test_scenarios_use_the_ids_and_params_d_already_sends(svc):
    sc = {s["id"]: s for s in svc.get("/sim/scenarios").json()}
    assert set(sc) == {"flash_crowd", "bot_swarm", "sybil_farm", "replica_kill"}
    swarm = sc["bot_swarm"]["params_schema"]["properties"]
    assert {"legit_users", "bots", "request_multiplier", "inventory", "mode", "defence_preset"} <= set(swarm)
    assert swarm["bots"]["default"] >= 1000 > 0 and swarm["inventory"]["default"] < swarm["bots"]["default"]  # option A
    assert swarm["mode"]["enum"] == ["LOTTERY", "FCFS"]
    assert "bot_identities" in sc["sybil_farm"]["params_schema"]["properties"]


def test_both_route_styles_work(svc):
    assert svc.get("/sim/health").json()["ok"] is True and svc.get("/health").json()["ok"] is True
    assert svc.get("/scenarios").json() == svc.get("/sim/scenarios").json()


@pytest.mark.parametrize("body,code,status", [
    ({"scenario_id": "nope", "target": "mock"}, "VALIDATION_ERROR", 400),
    ({"scenario_id": "flash_crowd", "target": "moon"}, "VALIDATION_ERROR", 400),
    ({"scenario_id": "flash_crowd", "target": "mock", "repeats": 0}, "VALIDATION_ERROR", 400),
    ({"scenario_id": "flash_crowd", "target": "mock", "repeats": 51}, "VALIDATION_ERROR", 400),
    ({"scenario_id": "flash_crowd", "target": "mock", "seed": -1}, "VALIDATION_ERROR", 400),
    ({"scenario_id": "bot_swarm", "target": "mock", "overrides": {"bots": -5}}, "VALIDATION_ERROR", 400),
    ({"scenario_id": "bot_swarm", "target": "mock", "overrides": {"mode": "RANDOM"}}, "VALIDATION_ERROR", 400),
    ({"scenario_id": "bot_swarm", "target": "mock", "overrides": {"mystery": 1}}, "VALIDATION_ERROR", 400),
    ({"scenario_id": "flash_crowd", "target": "mock", "overrides": {"legit_users": 12.5}}, "VALIDATION_ERROR", 400),
])
def test_bad_requests_are_rejected_with_a_message(svc, body, code, status):
    r = svc.post("/sim/runs", json=body)
    assert r.status_code == status and r.json()["code"] == code and r.json()["message"]


def test_real_target_down_is_a_showable_409(svc):
    r = svc.post("/sim/runs", json={"scenario_id": "flash_crowd", "target": "real"})
    assert r.status_code == 409 and r.json()["code"] == "TARGET_UNAVAILABLE"
    assert "not reachable" in r.json()["message"] and "mock" in r.json()["message"]


def test_chaos_scenario_is_listed_but_not_runnable_yet(svc):
    r = svc.post("/sim/runs", json={"scenario_id": "replica_kill", "target": "mock"})
    assert r.status_code == 409 and r.json()["code"] == "SCENARIO_UNAVAILABLE"
    assert "chaos" in r.json()["message"].lower()


def test_unknown_run_and_results_before_done(svc):
    assert svc.get("/sim/runs/nope").status_code == 404
    assert svc.get("/sim/runs/nope/results").json()["code"] == "NOT_FOUND"
    assert svc.get("/sim/runs/nope/stream").status_code == 404


def test_cancel_a_running_run(svc, done_run):
    rid = svc.post("/sim/runs", json={**SMALL, "overrides": {"legit_users": 100}}).json()["run_id"]
    assert svc.get(f"/sim/runs/{rid}/results").status_code == 409  # not ready yet
    wait_for(svc, rid, {"running"}, timeout=30)
    time.sleep(1.0)
    assert svc.post(f"/sim/runs/{rid}/cancel").status_code == 200
    v = wait_for(svc, rid, {"cancelled", "done", "failed"}, timeout=30)
    assert v["status"] == "cancelled" and v["finished_at"]
    # the worker survives a cancellation and takes the next run
    nxt = svc.post("/sim/runs", json=SMALL).json()["run_id"]
    assert wait_for(svc, nxt, {"done", "failed", "cancelled"})["status"] == "done"


def test_cancel_a_queued_run(svc):
    a = svc.post("/sim/runs", json=SMALL).json()["run_id"]
    b = svc.post("/sim/runs", json=SMALL).json()["run_id"]  # queued behind a
    assert svc.get(f"/sim/runs/{b}").json()["status"] == "queued"
    assert svc.post(f"/sim/runs/{b}/cancel").json()["status"] == "cancelled"
    wait_for(svc, a, {"done", "failed", "cancelled"})
    assert svc.get(f"/sim/runs/{b}").json()["status"] == "cancelled"  # never started


# --------------------------------------------------------------------------- charts & experiments


def test_sample_experiments_are_flagged_synthetic(svc):
    exps = svc.get("/sim/experiments").json()
    assert exps and all(e["synthetic"] is True and e["target"] == "mock" for e in exps)
    assert svc.get("/sim/experiments/synthetic-E1").json()["charts"] == ["bot_share_vs_request_multiplier"]
    assert svc.get("/sim/experiments/nope").status_code == 404


def test_chart_json_and_png(svc):
    c = svc.get("/sim/charts/bot_share_vs_request_multiplier?experiment=synthetic-E1").json()
    assert c["synthetic"] is True and c["series"]
    png = svc.get("/sim/charts/bot_share_vs_request_multiplier.png?experiment=synthetic-E1")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    assert svc.get("/sim/charts/nope?experiment=synthetic-E1").status_code == 404
    assert svc.get("/sim/charts/nope.png").status_code == 404


def test_real_experiments_are_listed_and_charts_are_per_experiment(svc):
    """Two experiments may share a chart_id (E5/E7); the experiment picks which one is served."""
    base = svc.results_dir / "experiments"
    for exp, title, y in (("E5", "Ablation", 0.1), ("E7", "Chaos", 0.9)):
        d = base / exp
        d.mkdir(parents=True, exist_ok=True)
        (d / "ablation_bot_share.json").write_text(json.dumps({
            "chart_id": "ablation_bot_share", "title": title, "x_label": "x", "y_label": "Bot seat share",
            "series": [{"name": "s", "points": [{"x": "a", "y": y}]}], "target": "real", "synthetic": False}), encoding="utf-8")
        (d / "experiment.json").write_text(json.dumps({
            "id": exp, "title": title, "description": "", "charts": ["ablation_bot_share"], "run_ids": [],
            "target": "real", "synthetic": False, "created_at": "2026-10-04T10:00:00+00:00"}), encoding="utf-8")
    ids = {e["id"]: e for e in svc.get("/sim/experiments").json()}
    assert ids["E5"]["synthetic"] is False and ids["E7"]["target"] == "real"
    assert svc.get("/sim/charts/ablation_bot_share?experiment=E5").json()["title"] == "Ablation"
    assert svc.get("/sim/charts/ablation_bot_share?experiment=E7").json()["title"] == "Chaos"
    real_png = svc.get("/sim/charts/ablation_bot_share.png?experiment=E5")
    assert real_png.status_code == 200 and real_png.content[:4] == b"\x89PNG"


@pytest.mark.parametrize("path", [
    "/sim/charts/..%2f..%2fsecrets?experiment=E5",
    "/sim/charts/ablation_bot_share?experiment=../E5",
    "/sim/charts/ablation_bot_share?experiment=..%5CE5",
    "/sim/charts/..%5C..%5Cx.png?experiment=E5",
])
def test_chart_paths_cannot_escape_the_results_dir(svc, path):
    assert svc.get(path).status_code in (404, 400)
