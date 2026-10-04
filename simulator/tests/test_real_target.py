"""Against Member A's REAL backend and Postgres. Skipped unless both are configured:

    $env:FD_REAL_URL = "http://127.0.0.1:8000"
    $env:FD_REAL_DSN = "postgresql://user:pw@host:5432/fairdrop"
    pytest tests/test_real_target.py

These pin the exact response shapes and behaviours the real adapters depend on (A's stage 2 at
origin/main 10277c8). A failure here means A's API or schema moved, which is the point: fix the
adapter (adapters/real_db.py, adapters/driver.py), not the test, unless A confirms the change.
They create events and sim users in that database; run them against a dev database only.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fairdrop_sim.adapters import real_db
from fairdrop_sim.doctor import FAIL, run_doctor
from fairdrop_sim.engine.run import TargetConfig, execute_run
from fairdrop_sim.metrics.aggregate import IncompleteRun, build_results
from fairdrop_sim.models import Scenario

URL = os.environ.get("FD_REAL_URL")
DSN = os.environ.get("FD_REAL_DSN")
ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "dev-admin-token")}
pytestmark = pytest.mark.skipif(not (URL and DSN), reason="set FD_REAL_URL and FD_REAL_DSN to run against A's real stack")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=URL, timeout=30) as c:
        yield c


def new_event(api: httpx.Client, inventory: int = 3) -> str:
    now = datetime.now(timezone.utc)
    r = api.post("/admin/events", headers=ADMIN, json={
        "name": "pytest real target", "inventory": inventory, "claim_ttl_seconds": 5,
        "window_opens_at": iso(now + timedelta(hours=1)), "window_closes_at": iso(now + timedelta(hours=2))})
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert api.post(f"/admin/events/{eid}/schedule", headers=ADMIN).status_code == 200
    return eid


def make_users(n: int, label: str = "human") -> list[str]:
    ids = [str(uuid.uuid4()) for _ in range(n)]
    real_db.insert_users(DSN, [(u, label) for u in ids])
    return ids


# --------------------------------------------------------------------------- the engine as it is today


def test_doctor_sees_a_real_engine_with_the_known_gaps():
    rep = asyncio.run(run_doctor(URL, ADMIN["X-Admin-Token"], dsn=DSN))
    assert rep.caps.kind == "real"
    assert not [c for c in rep.checks if c.name in ("db schema", "database", "admin token") and c.level == FAIL]
    for route in ("schedule", "open", "close", "enter", "status"):
        assert rep.caps.has(route), route


def test_schema_matches_what_the_simulator_reads():
    assert real_db.validate_schema(DSN) == []


def test_provisioning_is_idempotent_and_writes_labels():
    ids = [str(uuid.uuid4()) for _ in range(40)]
    users = [(u, "human" if i % 2 else "bot") for i, u in enumerate(ids)]
    assert real_db.insert_users(DSN, users) == 40
    assert real_db.insert_users(DSN, users) == 0  # a repeat run adds nothing
    import psycopg

    with psycopg.connect(DSN) as db:
        got = dict(db.execute("SELECT id::text, sim_label FROM users WHERE id = ANY(%s::uuid[])", (ids,)).fetchall())
    assert got == dict(users)


def test_enter_contract_as_the_clients_assume_it(api):
    """The shapes api_adapter/human.py rely on: an unknown user is 401, an unopened window is 409
    WINDOW_NOT_OPEN, enter is idempotent, status is a pure re-read, a closed window is 409 WINDOW_CLOSED."""
    eid = new_event(api)
    (uid,) = make_users(1)
    h = {"X-User-Id": uid, "X-Device-Id": "pytest"}
    assert api.post(f"/events/{eid}/enter", headers={"X-User-Id": str(uuid.uuid4())}).status_code == 401
    r = api.post(f"/events/{eid}/enter", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "WINDOW_NOT_OPEN"
    assert api.post(f"/admin/events/{eid}/open", headers=ADMIN).status_code == 200
    first = api.post(f"/events/{eid}/enter", headers=h).json()
    assert first["state"] == "ENTERED" and first["already_entered"] is False
    again = api.post(f"/events/{eid}/enter", headers=h).json()
    assert again["already_entered"] is True and again["entered_at"] == first["entered_at"]
    st = api.get(f"/events/{eid}/status", headers=h).json()
    assert st["state"] == "ENTERED" and st["phase"] == "OPEN"
    assert not {"public_id", "waitlist_position", "seat_no", "hold_expires_at"} & set(st)  # no rank leak pre-draw
    assert api.post(f"/admin/events/{eid}/close", headers=ADMIN).status_code == 200
    (late,) = make_users(1)
    r = api.post(f"/events/{eid}/enter", headers={"X-User-Id": late})
    assert r.status_code == 409 and r.json()["code"] == "WINDOW_CLOSED"
    assert api.post(f"/events/{eid}/enter", headers=h).json()["already_entered"] is True  # replay after close is answered


def test_public_event_list_is_an_envelope_not_a_bare_array(api):
    """D's A4 assumed a JSON array. A returns {events, server_now}. Pinned so a change is noticed."""
    body = api.get("/events").json()
    assert isinstance(body, dict) and "events" in body and "server_now" in body


# --------------------------------------------------------------------------- the independent integrity check


def test_independent_invariants_are_clean_on_a_clean_event_and_catch_planted_violations(api):
    import psycopg

    eid = new_event(api, inventory=2)
    users = make_users(3)
    api.post(f"/admin/events/{eid}/open", headers=ADMIN)
    for u in users:
        assert api.post(f"/events/{eid}/enter", headers={"X-User-Id": u}).status_code == 200
    clean = real_db.invariants(DSN, eid)
    assert clean["passed"] and set(clean["checks"].values()) == {0}

    with psycopg.connect(DSN, autocommit=True) as db:
        e1, e2 = [r[0] for r in db.execute("SELECT id FROM entries WHERE event_id=%s ORDER BY arrival_seq LIMIT 2",
                                           (eid,)).fetchall()]
        s1, s2 = [r[0] for r in db.execute("SELECT id FROM seats WHERE event_id=%s ORDER BY seat_no", (eid,)).fetchall()]
        # a CONFIRMED allocation for an entry that is still ENTERED: state/allocation mismatch (I4)
        db.execute("INSERT INTO allocations (event_id, entry_id, seat_id, status, confirmed_at, ticket_code) "
                   "VALUES (%s,%s,%s,'CONFIRMED', now(), %s)", (eid, e1, s1, f"PLANT-{uuid.uuid4().hex[:8]}"))
        # a HELD allocation long past its expiry that nobody swept (I5)
        db.execute("INSERT INTO allocations (event_id, entry_id, seat_id, status, held_at, hold_expires_at) "
                   "VALUES (%s,%s,%s,'HELD', now() - interval '10 minutes', now() - interval '5 minutes')",
                   (eid, e2, s2))
    dirty = real_db.invariants(DSN, eid)
    assert not dirty["passed"]
    assert dirty["checks"]["state_mismatch"] >= 1 and dirty["checks"]["orphaned_holds"] == 1, dirty["checks"]


# --------------------------------------------------------------------------- a whole (entry-only) run


def tiny() -> Scenario:
    return Scenario.model_validate({
        "name": "pytest_real_entry", "target": "real", "seed": 11, "repeats": 2,
        "event": {"inventory": 10, "window_seconds": 3, "claim_ttl_seconds": 3, "mode": "LOTTERY"},
        "legit": {"count": 120, "retry": {"retry_fraction": 0.2}, "poll": {"interval_s_mean": 1, "max_polls": 3}},
        "load": {"max_in_flight": 60, "procs": 1, "lead_s": 2, "sim_client_ip": False}})


def test_entry_only_run_end_to_end_loses_no_acknowledged_write_and_publishes_no_fairness(tmp_path):
    s = asyncio.run(execute_run(tiny(), 0, TargetConfig(base_url=URL, dsn=DSN), out_dir=tmp_path, log=lambda _m: None))
    o = s["outcomes"]
    assert s["target"] == "real" and s["entry_only"] is True and s["environment"]["kind"] == "real"
    assert o["human_entry_success_rate"] is not None and o["entered_client"] > 100
    assert o["entered_client"] == o["entered_server"]  # every user told "entered" has an entry, and nobody else does
    assert s["integrity"]["passed"] and s["integrity"]["draw_verified"] is None  # never a claimed True
    assert o["human_win_prob"] is None  # no draw => no seat could be won: null, not a convincing 0.0
    assert any("ENTRY-ONLY" in w for w in s["warnings"])
    with pytest.raises(IncompleteRun):
        build_results([s["artifacts"]], boot_resamples=100, perm_resamples=50)


def test_every_repeat_gets_a_fresh_event(tmp_path):
    a = asyncio.run(execute_run(tiny(), 0, TargetConfig(base_url=URL, dsn=DSN), out_dir=tmp_path, log=lambda _m: None))
    b = asyncio.run(execute_run(tiny(), 1, TargetConfig(base_url=URL, dsn=DSN), out_dir=tmp_path, log=lambda _m: None))
    assert a["event_id"] != b["event_id"] and uuid.UUID(a["event_id"]) and uuid.UUID(b["event_id"])
    assert a["run_seed"] != b["run_seed"]
