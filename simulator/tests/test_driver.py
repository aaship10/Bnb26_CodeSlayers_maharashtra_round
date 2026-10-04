"""AEngineDriver logic with fakes (no database, no server), and entry-only handling in the summary."""
from __future__ import annotations

import gzip
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fairdrop_sim.adapters import driver as drv
from fairdrop_sim.adapters.capabilities import Capabilities, from_openapi
from fairdrop_sim.crowd.population import build_bots, build_humans
from fairdrop_sim.metrics.aggregate import IncompleteRun, build_results
from fairdrop_sim.models import Scenario
from fairdrop_sim.models.scenario import NatGroups
from tests.test_metrics_aggregate import write_run

A_STAGE2 = json.loads((Path(__file__).parent / "fixtures" / "a_stage2_openapi.json").read_text(encoding="utf-8"))


class FakeAdmin:
    """Records admin calls; answers like A's create/schedule."""

    def __init__(self, invariants=None):
        self.calls: list[tuple[str, str, dict]] = []
        self._inv = invariants

    async def call(self, what, method, url, **kw):
        self.calls.append((method, url, kw))
        if url == "/admin/events":
            return {"id": "11111111-2222-3333-4444-555555555555", "phase": "DRAFT"}
        if url.endswith("/invariants"):
            return self._inv
        return {"ok": True}

    async def aclose(self):  # pragma: no cover
        pass


def real_caps(**routes) -> Capabilities:
    r = from_openapi(A_STAGE2)
    r.update(routes)
    return Capabilities(kind="real", reachable=True, base_url="http://a:8000", routes=r)


def scenario(**ev) -> Scenario:
    return Scenario.model_validate({"name": "t", "target": "real", "seed": 1,
                                    "event": {"inventory": 7, "claim_ttl_seconds": 4.2, "window_seconds": 5,
                                              "mode": "LOTTERY", **ev},
                                    "legit": {"count": 10}, "load": {"claim_phase_s": 9}})


@pytest.mark.asyncio
async def test_prepare_event_sends_a_real_event_shape_and_schedules_it():
    admin = FakeAdmin()
    d = drv.AEngineDriver(admin, real_caps(), dsn="postgresql://x")
    eid = await d.prepare_event(scenario(), run_index=3, seed_hex="ab" * 32, hint="ignored")
    assert eid == "11111111-2222-3333-4444-555555555555"  # A's UUID, not our hint
    (m1, u1, kw1), (m2, u2, _) = admin.calls
    assert (m1, u1) == ("POST", "/admin/events") and (m2, u2) == ("POST", f"/admin/events/{eid}/schedule")
    body = kw1["json"]
    assert body["inventory"] == 7 and body["mode"] == "LOTTERY" and body["name"].startswith("SIM t r03")
    assert body["claim_ttl_seconds"] == math.ceil(4.2) == 5  # A wants integer seconds
    assert body["claim_phase_seconds"] == 9
    assert "server_seed_hex" not in body  # A owns the seed (commit-reveal); we never choose it
    opens = datetime.fromisoformat(body["window_opens_at"].replace("Z", "+00:00"))
    closes = datetime.fromisoformat(body["window_closes_at"].replace("Z", "+00:00"))
    assert timedelta(minutes=50) < opens - datetime.now(timezone.utc) < timedelta(minutes=70)  # placeholder window
    assert closes - opens == timedelta(hours=1) and body["config"]["defences"]["preset"] == "none"


def test_real_driver_uses_manual_window_and_mock_does_not():
    assert drv.AEngineDriver.manual_window is True and drv.MockDriver.manual_window is False


@pytest.mark.asyncio
async def test_no_database_means_a_showable_error_not_a_crash():
    d = drv.AEngineDriver(FakeAdmin(), real_caps(), dsn=None)
    humans = build_humans(5, NatGroups(), 1)
    with pytest.raises(drv.DriverUnavailable, match="FD_REAL_DSN"):
        await d.provision(humans, [])
    with pytest.raises(drv.DriverUnavailable, match="FD_REAL_DSN"):
        await d.progress()


@pytest.mark.asyncio
async def test_draw_is_skipped_when_the_engine_has_none_and_for_fcfs():
    d = drv.AEngineDriver(FakeAdmin(), real_caps(), dsn="x")
    assert await d.draw(scenario()) is None  # A stage 2: no draw route
    d2 = drv.AEngineDriver(FakeAdmin(), real_caps(draw=True), dsn="x")
    assert await d2.draw(scenario(mode="FCFS")) is None  # FCFS has no draw
    assert len(d2.admin.calls) == 0


@pytest.mark.asyncio
async def test_invariants_need_both_checkers_when_a_has_one(monkeypatch):
    from fairdrop_sim.adapters import real_db

    mine = {"passed": True, "checks": {"oversold": 0}, "source": "c_sql(I1-I7)"}
    monkeypatch.setattr(real_db, "invariants", lambda dsn, eid: mine)
    clean = drv.AEngineDriver(FakeAdmin({"passed": True, "oversold": 0}), real_caps(invariants=True), dsn="x")
    clean.event_id = "e"
    assert (await clean.invariants())["passed"] is True
    dirty = drv.AEngineDriver(FakeAdmin({"passed": False, "oversold": 2}), real_caps(invariants=True), dsn="x")
    dirty.event_id = "e"
    out = await dirty.invariants()
    assert out["passed"] is False and out["checks"]["a_oversold"] == 2  # A's counter is surfaced, not dropped
    alone = drv.AEngineDriver(FakeAdmin(), real_caps(), dsn="x")
    alone.event_id = "e"
    assert (await alone.invariants())["source"] == "c_sql(I1-I7)"  # no A endpoint: ours only, and says so


@pytest.mark.asyncio
async def test_verifier_is_null_unless_configured_and_honest_when_it_runs(monkeypatch):
    d = drv.AEngineDriver(FakeAdmin(), real_caps(), dsn="x")
    d.event_id = "abc"
    monkeypatch.delenv("FD_VERIFY_CMD", raising=False)
    assert await d._verify() is None  # never a claimed True
    monkeypatch.setenv("FD_VERIFY_CMD", f'"{sys.executable}" -c "import sys; sys.exit(0)" {{event_id}}')
    assert (await d._verify())["verified"] is True
    monkeypatch.setenv("FD_VERIFY_CMD", f'"{sys.executable}" -c "import sys; sys.exit(3)" {{event_id}}')
    bad = await d._verify()
    assert bad["verified"] is False and bad["exit_code"] == 3


def test_real_users_are_provisioned_with_labels_without_reading_any(monkeypatch):
    from fairdrop_sim.adapters import real_db

    seen = {}
    monkeypatch.setattr(real_db, "validate_schema", lambda dsn: [])
    monkeypatch.setattr(real_db, "insert_users", lambda dsn, users: seen.setdefault("users", list(users)) and 0)
    import asyncio

    d = drv.AEngineDriver(FakeAdmin(), real_caps(), dsn="x")
    humans, bots = build_humans(4, NatGroups(), 9), build_bots(0, 3, 1, 9)
    out = asyncio.run(d.provision(humans, [bots]))
    labels = [lbl for _, lbl in seen["users"]]
    assert labels.count("human") == 4 and labels.count("bot") == 3 and out["provisioned"] == 7
    assert {u for u, _ in seen["users"]} == set(humans.user_ids) | set(bots.user_ids)


def test_a_schema_mismatch_names_the_missing_columns(monkeypatch):
    from fairdrop_sim.adapters import real_db
    import asyncio

    monkeypatch.setattr(real_db, "validate_schema", lambda dsn: ["entries.draw_rank", "seats.seat_no"])
    d = drv.AEngineDriver(FakeAdmin(), real_caps(), dsn="x")
    with pytest.raises(drv.DriverUnavailable, match=r"entries\.draw_rank, seats\.seat_no"):
        asyncio.run(d.provision(build_humans(2, NatGroups(), 1), []))


# --------------------------------------------------------------------------- entry-only is never aggregated


def test_entry_only_runs_cannot_become_a_results_file(tmp_path):
    ok = write_run(tmp_path / "a", seed=1, bot_seats=0, bot_entrants=0, human_entrants=50, human_seats=0, cost_requests=0)
    meta = json.loads((ok / "run.json").read_text(encoding="utf-8"))
    meta["entry_only"] = True
    (ok / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(IncompleteRun, match="ENTRY-ONLY"):
        build_results([ok], boot_resamples=100, perm_resamples=50)
    meta["entry_only"] = False  # the same run, once the engine can draw, aggregates normally
    (ok / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    assert build_results([ok], boot_resamples=100, perm_resamples=50).repeats == 1
