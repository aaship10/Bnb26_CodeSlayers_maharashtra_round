"""Stage 2: admin endpoints, scheduling, idempotency, auth."""
import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.services.beacon import MockBeacon
from tests.conftest import ADMIN, create_event, iso

pytestmark = pytest.mark.db


async def test_admin_token_required(api):
    r = await api.get("/admin/events")
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
    r = await api.get("/admin/events", headers={"X-Admin-Token": "nope"})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"


async def test_create_validation_errors(api):
    now = datetime.now(UTC)
    body = {"name": "x", "inventory": 5, "window_opens_at": iso(now), "window_closes_at": iso(now)}
    r = await api.post("/admin/events", headers=ADMIN, json=body)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    body["window_closes_at"] = "2026-10-03T12:00:00"  # naive datetime rejected
    r = await api.post("/admin/events", headers=ADMIN, json=body)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    r = await api.post("/admin/events", headers=ADMIN, json={**body, "inventory": 0})
    assert r.status_code == 422


async def test_schedule_creates_seats_commitment_and_beacon_round(api, db):
    ev = await create_event(api, inventory=7, schedule=False)
    assert ev["phase"] == "DRAFT" and ev["seed_commitment"] is None
    r = await api.post(f"/admin/events/{ev['id']}/schedule", headers=ADMIN)
    body = r.json()
    assert r.status_code == 200 and body["changed"] is True
    sched = body["event"]
    assert sched["phase"] == "SCHEDULED"

    seed_hex = db.execute("SELECT server_seed FROM event_secrets WHERE event_id = %s", (ev["id"],)).fetchone()[0]
    assert sched["seed_commitment"] == hashlib.sha256(bytes.fromhex(seed_hex)).hexdigest()
    closes = datetime.fromisoformat(sched["window_closes_at"])
    assert sched["beacon_round"] == MockBeacon().round_after(closes)
    assert MockBeacon().round_time(sched["beacon_round"]) > closes
    assert db.execute("SELECT count(*), min(seat_no), max(seat_no) FROM seats WHERE event_id = %s",
                      (ev["id"],)).fetchone() == (7, 1, 7)
    expected_end = closes + timedelta(seconds=sched["claim_phase_seconds"])
    assert datetime.fromisoformat(sched["claim_phase_ends_at"]) == expected_end

    # The secret seed never appears in any API response before the reveal.
    for path in (f"/admin/events/{ev['id']}", f"/events/{ev['id']}", "/admin/events", "/events"):
        text = (await api.get(path, headers=ADMIN)).text
        assert seed_hex not in text

    # Idempotent: second schedule is a no-op with the same commitment.
    r2 = (await api.post(f"/admin/events/{ev['id']}/schedule", headers=ADMIN)).json()
    assert r2["changed"] is False and r2["event"]["seed_commitment"] == sched["seed_commitment"]


async def test_concurrent_schedule_happens_once(api, db):
    ev = await create_event(api, inventory=50, schedule=False)
    rs = await asyncio.gather(*[api.post(f"/admin/events/{ev['id']}/schedule", headers=ADMIN) for _ in range(10)])
    assert all(r.status_code == 200 for r in rs)
    assert sum(r.json()["changed"] for r in rs) == 1
    assert len({r.json()["event"]["seed_commitment"] for r in rs}) == 1
    assert db.execute("SELECT count(*) FROM seats WHERE event_id = %s", (ev["id"],)).fetchone()[0] == 50
    assert db.execute("SELECT count(*) FROM event_secrets WHERE event_id = %s", (ev["id"],)).fetchone()[0] == 1


async def test_fcfs_schedule_has_no_seed(api, db):
    ev = await create_event(api, mode="FCFS", inventory=3)
    assert ev["phase"] == "SCHEDULED" and ev["seed_commitment"] is None and ev["beacon_round"] is None
    assert db.execute("SELECT count(*) FROM event_secrets WHERE event_id = %s", (ev["id"],)).fetchone()[0] == 0


async def test_cannot_schedule_after_window(api):
    ev = await create_event(api, opens_in=-120, closes_in=-60, schedule=False)
    r = await api.post(f"/admin/events/{ev['id']}/schedule", headers=ADMIN)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


async def test_open_and_close_overrides(api):
    ev = await create_event(api, opens_in=3600, closes_in=7200)
    r = (await api.post(f"/admin/events/{ev['id']}/open", headers=ADMIN)).json()
    assert r["changed"] and r["event"]["phase"] == "OPEN" and r["event"]["window_status"] == "OPEN"
    assert datetime.fromisoformat(r["event"]["window_opens_at"]) <= datetime.fromisoformat(r["event"]["server_now"])
    assert (await api.post(f"/admin/events/{ev['id']}/open", headers=ADMIN)).json()["changed"] is False

    r = (await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)).json()
    assert r["changed"] and r["event"]["phase"] == "DRAWING" and r["event"]["window_status"] == "CLOSED"
    assert (await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)).json()["changed"] is False
    # Cannot reopen after close.
    assert (await api.post(f"/admin/events/{ev['id']}/open", headers=ADMIN)).json()["changed"] is False


async def test_close_before_open_keeps_window_valid(api):
    ev = await create_event(api, opens_in=3600, closes_in=7200)
    r = (await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)).json()["event"]
    assert r["window_opens_at"] < r["window_closes_at"]


async def test_open_requires_schedule(api):
    ev = await create_event(api, schedule=False)
    r = await api.post(f"/admin/events/{ev['id']}/open", headers=ADMIN)
    assert r.status_code == 409 and r.json()["code"] == "INVALID_PHASE"


async def test_fcfs_close_goes_to_closed(api):
    ev = await create_event(api, mode="FCFS")
    r = (await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)).json()["event"]
    assert r["phase"] == "CLOSED" and r["closed_at"] is not None


async def test_patch_config(api):
    ev = await create_event(api, schedule=False)
    r = await api.patch(f"/admin/events/{ev['id']}/config", headers=ADMIN,
                        json={"mode": "FCFS", "claim_ttl_seconds": 30})
    assert r.status_code == 200 and r.json()["mode"] == "FCFS" and r.json()["claim_ttl_seconds"] == 30
    await api.patch(f"/admin/events/{ev['id']}/config", headers=ADMIN, json={"mode": "LOTTERY"})
    await api.post(f"/admin/events/{ev['id']}/schedule", headers=ADMIN)
    # config may change in any phase; mode/timings only while DRAFT.
    r = await api.patch(f"/admin/events/{ev['id']}/config", headers=ADMIN, json={"config": {"pow": {"bits": 18}}})
    assert r.status_code == 200 and r.json()["config"] == {"pow": {"bits": 18}}
    r = await api.patch(f"/admin/events/{ev['id']}/config", headers=ADMIN, json={"mode": "FCFS"})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_PHASE"
    r = await api.patch(f"/admin/events/{ev['id']}/config", headers=ADMIN, json={"bogus": 1})
    assert r.status_code == 422


async def test_admin_idempotency_key(api, db):
    now = datetime.now(UTC)
    body = {"name": "idem", "inventory": 3, "window_opens_at": iso(now),
            "window_closes_at": iso(now + timedelta(minutes=5))}
    h = {**ADMIN, "Idempotency-Key": "create-1"}
    r1 = await api.post("/admin/events", headers=h, json=body)
    r2 = await api.post("/admin/events", headers=h, json=body)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json() and r2.headers.get("Idempotent-Replayed") == "true"
    r3 = await api.post("/admin/events", headers=h, json={**body, "inventory": 4})
    assert r3.status_code == 422 and r3.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    # Concurrent requests with one key create exactly one event.
    h2 = {**ADMIN, "Idempotency-Key": "create-2"}
    rs = await asyncio.gather(*[api.post("/admin/events", headers=h2, json=body) for _ in range(8)])
    assert len({r.json()["id"] for r in rs}) == 1
    assert db.execute("SELECT count(*) FROM events WHERE name = 'idem'").fetchone()[0] == 2


async def test_public_event_views(api):
    draft = await create_event(api, schedule=False)
    ev = await create_event(api, config={"secret_toggle": True})
    listed = (await api.get("/events")).json()
    ids = [e["id"] for e in listed["events"]]
    assert ev["id"] in ids and draft["id"] not in ids
    assert (await api.get(f"/events/{draft['id']}")).json()["code"] == "NOT_FOUND"
    pub = (await api.get(f"/events/{ev['id']}")).json()
    assert pub["seed_commitment"] == ev["seed_commitment"] and pub["window_status"] == "OPEN"
    for hidden in ("config", "final_seed", "revealed_seed", "entrants_hash", "beacon_randomness"):
        assert hidden not in pub
    assert "server_now" in pub and "server_now" in listed


async def test_unknown_route_and_event_use_error_envelope(api):
    r = await api.get("/events/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    r = await api.get("/events/not-a-uuid")
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
