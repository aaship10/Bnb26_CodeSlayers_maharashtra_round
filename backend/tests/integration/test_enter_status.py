"""Stage 2: dev auth, idempotent /enter with window enforcement, gate hook, /status."""
import asyncio
import uuid

import pytest

from app import hooks
from app.hooks import GateAction, GateDecision
from tests.conftest import ADMIN, create_event, make_users

pytestmark = pytest.mark.db


def U(uid) -> dict:
    return {"X-User-Id": str(uid)}


# ------------------------------------------------------------------ auth
async def test_auth_required_and_validated(api, db):
    ev = await create_event(api)
    for headers in ({}, {"X-User-Id": "nope"}, U(uuid.uuid4()), {"Authorization": "Bearer dev.x.y"}):
        r = await api.post(f"/events/{ev['id']}/enter", headers=headers)
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED", headers


async def test_dev_login_token(api, db):
    ev = await create_event(api)
    r = await api.post("/auth/dev-login", json={"email": "Alice@Example.com"})
    tok, uid = r.json()["token"], r.json()["user_id"]
    again = (await api.post("/auth/dev-login", json={"email": "alice@example.com"})).json()
    assert again["user_id"] == uid  # find-or-create, email normalised
    r = await api.post(f"/events/{ev['id']}/enter", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["state"] == "ENTERED"
    forged = tok[:-1] + ("0" if tok[-1] != "0" else "1")
    r = await api.get(f"/events/{ev['id']}/status", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


# ------------------------------------------------------------------ enter
async def test_enter_then_duplicate(api, db):
    ev = await create_event(api)
    [u] = make_users(db, 1)
    r1 = (await api.post(f"/events/{ev['id']}/enter", headers=U(u))).json()
    r2 = (await api.post(f"/events/{ev['id']}/enter", headers=U(u))).json()
    assert r1["state"] == "ENTERED" and r1["already_entered"] is False
    assert r2["already_entered"] is True and r2["entered_at"] == r1["entered_at"]


async def test_1000_concurrent_duplicate_enters_create_one_row(api, db):
    ev = await create_event(api)
    [u] = make_users(db, 1)
    rs = await asyncio.gather(*[api.post(f"/events/{ev['id']}/enter", headers=U(u)) for _ in range(1000)])
    assert all(r.status_code == 200 for r in rs), {r.status_code for r in rs}
    assert sum(not r.json()["already_entered"] for r in rs) == 1
    assert len({r.json()["entered_at"] for r in rs}) == 1
    assert db.execute("SELECT count(*) FROM entries WHERE event_id = %s", (ev["id"],)).fetchone()[0] == 1


async def test_window_not_open_and_closed(api, db):
    [u] = make_users(db, 1)
    future = await create_event(api, opens_in=3600, closes_in=7200)
    r = await api.post(f"/events/{future['id']}/enter", headers=U(u))
    assert r.status_code == 409 and r.json()["code"] == "WINDOW_NOT_OPEN"
    assert "server_now" in r.json()["details"]

    ev = await create_event(api, opens_in=-60, closes_in=30)
    db.execute("UPDATE dev_clock SET offset_seconds = 31 WHERE id")  # server time passes the close
    r = await api.post(f"/events/{ev['id']}/enter", headers=U(u))
    assert r.status_code == 409 and r.json()["code"] == "WINDOW_CLOSED"
    assert db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


async def test_enter_draft_event_is_not_found(api, db):
    [u] = make_users(db, 1)
    ev = await create_event(api, schedule=False)
    r = await api.post(f"/events/{ev['id']}/enter", headers=U(u))
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


async def test_enter_after_manual_close(api, db):
    [u, v] = make_users(db, 2)
    ev = await create_event(api)
    assert (await api.post(f"/events/{ev['id']}/enter", headers=U(u))).status_code == 200
    await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)
    r = await api.post(f"/events/{ev['id']}/enter", headers=U(v))
    assert r.status_code == 409 and r.json()["code"] == "WINDOW_CLOSED"
    # The earlier entrant still gets an idempotent answer after the close.
    r = await api.post(f"/events/{ev['id']}/enter", headers=U(u))
    assert r.status_code == 200 and r.json()["already_entered"] is True


async def test_entries_racing_a_close_stay_inside_the_window(api, db):
    """Entries in flight while an admin closes the window are either recorded
    inside the window or refused; the close never "misses" one (I10)."""
    ev = await create_event(api)
    users = make_users(db, 300)

    async def close_soon():
        await asyncio.sleep(0.3)
        return await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)

    results = await asyncio.gather(close_soon(), *[
        api.post(f"/events/{ev['id']}/enter", headers=U(u)) for u in users])
    close_r, enters = results[0], results[1:]
    assert close_r.status_code == 200
    codes = {r.status_code for r in enters}
    assert codes <= {200, 409}
    assert all(r.json()["code"] == "WINDOW_CLOSED" for r in enters if r.status_code == 409)
    ok = sum(r.status_code == 200 for r in enters)
    print(f"race: {ok} entered before close, {len(enters) - ok} refused")
    row = db.execute("""
        SELECT count(*), count(*) FILTER (WHERE en.entered_at < ev.window_opens_at
                                              OR en.entered_at > ev.window_closes_at)
          FROM entries en JOIN events ev ON ev.id = en.event_id WHERE ev.id = %s
    """, (ev["id"],)).fetchone()
    assert row == (ok, 0)


async def test_manual_open_allows_entry_early(api, db):
    [u] = make_users(db, 1)
    ev = await create_event(api, opens_in=3600, closes_in=7200)
    await api.post(f"/admin/events/{ev['id']}/open", headers=ADMIN)
    r = await api.post(f"/events/{ev['id']}/enter", headers=U(u))
    assert r.status_code == 200
    opens = db.execute("SELECT window_opens_at FROM events WHERE id = %s", (ev["id"],)).fetchone()[0]
    entered = db.execute("SELECT entered_at FROM entries WHERE user_id = %s", (u,)).fetchone()[0]
    assert entered >= opens


# ------------------------------------------------------------------ gate hook
async def test_gate_weight_and_risk_are_stored(api, db):
    async def gate(ctx):
        assert ctx.user.id and ctx.event.config == {"mode": "strict"} and ctx.already_entered is False
        return GateDecision(weight=0.5, risk={"score": 0.7})
    hooks.register_entry_gate(gate)
    ev = await create_event(api, config={"mode": "strict"})
    [u] = make_users(db, 1)
    assert (await api.post(f"/events/{ev['id']}/enter", headers=U(u))).status_code == 200
    w, risk = db.execute("SELECT weight, risk FROM entries WHERE user_id = %s", (u,)).fetchone()
    assert float(w) == 0.5 and risk == {"score": 0.7}


async def test_gate_challenge_and_reject(api, db):
    ev = await create_event(api)
    [a, b] = make_users(db, 2)

    async def gate(ctx):
        if ctx.user.id == a:
            return GateDecision(action=GateAction.CHALLENGE, challenge={"type": "pow", "bits": 20})
        return GateDecision(action=GateAction.REJECT, reason="secret-rule-17")
    hooks.register_entry_gate(gate)

    r = await api.post(f"/events/{ev['id']}/enter", headers=U(a))
    assert r.status_code == 403 and r.json()["code"] == "CHALLENGE_REQUIRED"
    assert r.json()["details"]["challenge"] == {"type": "pow", "bits": 20}
    r = await api.post(f"/events/{ev['id']}/enter", headers=U(b))
    assert r.status_code == 403 and r.json()["code"] == "REJECTED"
    assert "secret-rule-17" not in r.text
    assert db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


async def test_gate_not_called_for_duplicates(api, db):
    calls = 0

    async def gate(ctx):
        nonlocal calls
        calls += 1
        return GateDecision()
    hooks.register_entry_gate(gate)
    ev = await create_event(api)
    [u] = make_users(db, 1)
    for _ in range(5):
        await api.post(f"/events/{ev['id']}/enter", headers=U(u))
    assert calls == 1


async def test_gate_failure_fails_closed(api, db):
    async def gate(ctx):
        raise RuntimeError("redis down")
    hooks.register_entry_gate(gate)
    ev = await create_event(api)
    [u] = make_users(db, 1)
    r = await api.post(f"/events/{ev['id']}/enter", headers=U(u))
    assert r.status_code == 500 and r.json()["code"] == "INTERNAL"
    assert db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


# ------------------------------------------------------------------ status
async def test_status_lifecycle_and_no_predraw_leaks(api, db):
    ev = await create_event(api, inventory=2)
    [u] = make_users(db, 1)
    s = (await api.get(f"/events/{ev['id']}/status", headers=U(u))).json()
    assert s["state"] == "REGISTERED" and "server_now" in s and s["phase"] == "SCHEDULED"

    await api.post(f"/events/{ev['id']}/enter", headers=U(u))
    s = (await api.get(f"/events/{ev['id']}/status", headers=U(u))).json()
    assert s["state"] == "ENTERED"
    for leaked in ("public_id", "waitlist_position", "hold_expires_at", "seat_no", "draw_rank", "weight", "rank"):
        assert leaked not in s

    # Simulate a completed draw (stage 3 does this for real): user is waitlisted.
    db.execute("UPDATE events SET drawn_at = fd_now() WHERE id = %s", (ev["id"],))
    db.execute("UPDATE entries SET state = 'WAITLISTED', draw_rank = 3, waitlist_position = 1 WHERE user_id = %s", (u,))
    s = (await api.get(f"/events/{ev['id']}/status", headers=U(u))).json()
    pid = db.execute("SELECT public_id FROM entries WHERE user_id = %s", (u,)).fetchone()[0]
    assert s["state"] == "WAITLISTED" and s["waitlist_position"] == 1 and s["public_id"] == str(pid)
    assert "draw_rank" not in s and "weight" not in s

    # Promoted with a hold, then claimed.
    entry_id = db.execute("SELECT id FROM entries WHERE user_id = %s", (u,)).fetchone()[0]
    seat = db.execute("SELECT id FROM seats WHERE event_id = %s AND seat_no = 2", (ev["id"],)).fetchone()[0]
    db.execute("UPDATE entries SET state = 'WON' WHERE id = %s", (entry_id,))
    alloc = db.execute("""INSERT INTO allocations (event_id, entry_id, seat_id, status, held_at, hold_expires_at)
                          VALUES (%s, %s, %s, 'HELD', fd_now(), fd_now() + interval '1 minute') RETURNING id""",
                       (ev["id"], entry_id, seat)).fetchone()[0]
    s = (await api.get(f"/events/{ev['id']}/status", headers=U(u))).json()
    assert s["state"] == "WON" and "hold_expires_at" in s and "waitlist_position" not in s and "seat_no" not in s

    db.execute("UPDATE allocations SET status='CONFIRMED', confirmed_at=fd_now(), ticket_code='T-1' WHERE id=%s", (alloc,))
    db.execute("UPDATE entries SET state = 'CLAIMED' WHERE id = %s", (entry_id,))
    s = (await api.get(f"/events/{ev['id']}/status", headers=U(u))).json()
    assert s["state"] == "CLAIMED" and s["seat_no"] == 2 and s["ticket_code"] == "T-1"


async def test_server_now_follows_dev_clock(api, db):
    ev = await create_event(api)
    t0 = (await api.get(f"/events/{ev['id']}")).json()["server_now"]
    db.execute("UPDATE dev_clock SET offset_seconds = 3600 WHERE id")
    t1 = (await api.get(f"/events/{ev['id']}")).json()["server_now"]
    from datetime import datetime
    assert (datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds() > 3500
