"""Stage 4: claim, hold expiry, waitlist promotion, end of the claim phase, worker, invariants."""
import asyncio
from uuid import UUID

import pytest

from app import worker
from app.db import get_engine
from app.services import lifecycle
from tests.conftest import ADMIN, create_event, make_users

pytestmark = pytest.mark.db


def U(uid) -> dict:
    return {"X-User-Id": str(uid)}


def advance(db, seconds: float) -> None:
    db.execute("UPDATE dev_clock SET offset_seconds = offset_seconds + %s WHERE id", (seconds,))


async def drawn_event(api, db, n_users: int, inventory: int, ttl: int = 60):
    users = make_users(db, n_users)
    ev = await create_event(api, inventory=inventory, ttl=ttl)
    rs = await asyncio.gather(*[api.post(f"/events/{ev['id']}/enter", headers=U(u)) for u in users])
    assert all(r.status_code == 200 for r in rs)
    assert (await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)).status_code == 200
    r = await api.post(f"/admin/events/{ev['id']}/draw", headers=ADMIN)
    assert r.status_code == 200, r.text
    return ev, users


def by_state(db, state: str, order: str = "draw_rank"):
    return [r[0] for r in db.execute(
        f"SELECT user_id FROM entries WHERE state = %s ORDER BY {order}", (state,)).fetchall()]


async def sweep(ev) -> lifecycle.SweepResult:
    async with get_engine().connect() as conn:
        return await lifecycle.sweep_event(conn, UUID(ev["id"]))


async def invariants(api, ev) -> dict:
    r = await api.get(f"/admin/events/{ev['id']}/invariants", headers=ADMIN)
    assert r.status_code == 200
    return r.json()


async def claim(api, ev, user, key=None):
    return await api.post(f"/events/{ev['id']}/claim", headers={**U(user), **({"Idempotency-Key": key} if key else {})})


# ------------------------------------------------------------------ claim
async def test_winner_claims_and_status_shows_ticket(api, db):
    ev, users = await drawn_event(api, db, 8, inventory=3)
    w = by_state(db, "WON")[0]
    r = await claim(api, ev, w)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "CLAIMED" and body["seat_no"] >= 1 and body["ticket_code"].startswith("FD-")
    s = (await api.get(f"/events/{ev['id']}/status", headers=U(w))).json()
    assert s["state"] == "CLAIMED" and s["ticket_code"] == body["ticket_code"] and s["seat_no"] == body["seat_no"]
    assert "hold_expires_at" not in s
    assert db.execute("SELECT status FROM allocations WHERE ticket_code = %s", (body["ticket_code"],)).fetchone() == ("CONFIRMED",)
    assert (await invariants(api, ev))["passed"] is True


async def test_not_winner_cases(api, db):
    ev, users = await drawn_event(api, db, 6, inventory=2)
    waitlisted = by_state(db, "WAITLISTED")[0]
    r = await claim(api, ev, waitlisted)
    assert r.status_code == 403 and r.json()["code"] == "NOT_WINNER"
    [stranger] = make_users(db, 1)
    r = await claim(api, ev, stranger)
    assert r.status_code == 403 and r.json()["code"] == "NOT_WINNER"


async def test_claim_before_draw_is_invalid_phase(api, db):
    [u] = make_users(db, 1)
    ev = await create_event(api)
    await api.post(f"/events/{ev['id']}/enter", headers=U(u))
    r = await claim(api, ev, u)
    assert r.status_code == 409 and r.json()["code"] == "INVALID_PHASE"
    await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)       # DRAWING, not drawn
    r = await claim(api, ev, u)
    assert r.status_code == 409 and r.json()["code"] == "INVALID_PHASE"


async def test_idempotent_replay_and_second_claim(api, db):
    ev, _ = await drawn_event(api, db, 5, inventory=2)
    w = by_state(db, "WON")[0]
    r1 = await claim(api, ev, w, key="k1")
    r2 = await claim(api, ev, w, key="k1")
    assert r1.status_code == r2.status_code == 200
    assert r2.json() == r1.json() and r2.headers["Idempotent-Replayed"] == "true"
    r3 = await claim(api, ev, w, key="other")
    assert r3.status_code == 409 and r3.json()["code"] == "ALREADY_CLAIMED"
    r4 = await claim(api, ev, w)
    assert r4.status_code == 409 and r4.json()["code"] == "ALREADY_CLAIMED"
    assert db.execute("SELECT count(*) FROM allocations WHERE status = 'CONFIRMED'").fetchone()[0] == 1


async def test_concurrent_claims_issue_exactly_one_ticket(api, db):
    ev, _ = await drawn_event(api, db, 4, inventory=1)
    w = by_state(db, "WON")[0]
    same_key = await asyncio.gather(*[claim(api, ev, w, key="same") for _ in range(30)])
    assert {r.status_code for r in same_key} == {200}
    assert len({r.json()["ticket_code"] for r in same_key}) == 1
    ev2, _ = await drawn_event(api, db, 4, inventory=1)
    # fresh event; same user id cannot be reused across tests' wipes, so use its own winner
    w2 = db.execute("SELECT user_id FROM entries WHERE event_id = %s AND state = 'WON'", (ev2["id"],)).fetchone()[0]
    many_keys = await asyncio.gather(*[claim(api, ev2, w2, key=f"k{i}") for i in range(30)])
    codes = sorted(r.status_code for r in many_keys)
    assert codes == [200] + [409] * 29
    assert db.execute("SELECT count(*) FROM allocations WHERE event_id = %s AND status = 'CONFIRMED'",
                      (ev2["id"],)).fetchone()[0] == 1


# ------------------------------------------------------------------ expiry + promotion
async def test_expired_hold_cannot_claim_even_before_sweep(api, db):
    ev, _ = await drawn_event(api, db, 4, inventory=1, ttl=60)
    w = by_state(db, "WON")[0]
    advance(db, 61)
    r = await claim(api, ev, w)
    assert r.status_code == 410 and r.json()["code"] == "HOLD_EXPIRED"
    assert db.execute("SELECT count(*) FROM allocations WHERE status = 'CONFIRMED'").fetchone()[0] == 0


async def test_sweep_expires_and_promotes_in_waitlist_order(api, db):
    ev, _ = await drawn_event(api, db, 6, inventory=2, ttl=60)
    first_winners = by_state(db, "WON")
    waitlist = by_state(db, "WAITLISTED", order="waitlist_position")
    assert await sweep(ev) == lifecycle.SweepResult()          # nothing due yet
    advance(db, 61)
    res = await sweep(ev)
    assert (res.expired, res.promoted, res.closed) == (2, 2, False)
    assert sorted(by_state(db, "EXPIRED")) == sorted(first_winners)
    assert by_state(db, "WON", order="draw_rank") == waitlist[:2]            # next in line, in order
    s = (await api.get(f"/events/{ev['id']}/status", headers=U(waitlist[0]))).json()
    assert s["state"] == "WON" and s["hold_expires_at"] > s["server_now"]
    assert (await invariants(api, ev))["passed"] is True
    r = await claim(api, ev, waitlist[0])
    assert r.status_code == 200
    # and the people who lost their hold are told so
    r = await claim(api, ev, first_winners[0])
    assert r.status_code == 410 and r.json()["code"] == "HOLD_EXPIRED"


async def test_claimed_seats_are_never_touched_by_the_sweep(api, db):
    ev, _ = await drawn_event(api, db, 6, inventory=2, ttl=60)
    w1, w2 = by_state(db, "WON")
    ticket = (await claim(api, ev, w1)).json()
    advance(db, 61)
    res = await sweep(ev)
    assert (res.expired, res.promoted) == (1, 1)
    assert db.execute("SELECT state FROM entries WHERE user_id = %s", (w1,)).fetchone() == ("CLAIMED",)
    held = db.execute("SELECT s.seat_no FROM allocations a JOIN seats s ON s.id = a.seat_id "
                      "WHERE a.status = 'HELD'").fetchall()
    assert held != [] and all(seat != ticket["seat_no"] for (seat,) in held)
    assert (await invariants(api, ev))["passed"] is True


async def test_waitlist_cascades_until_exhausted_then_seats_stay_free(api, db):
    ev, _ = await drawn_event(api, db, 4, inventory=2, ttl=60)       # 2 winners, 2 waitlisted
    advance(db, 61)
    assert (await sweep(ev)).promoted == 2
    advance(db, 61)
    res = await sweep(ev)                                            # promoted holds lapse; nobody left
    assert (res.expired, res.promoted) == (2, 0)
    assert sorted(by_state(db, "EXPIRED")) and db.execute(
        "SELECT count(*) FROM entries WHERE state = 'EXPIRED'").fetchone()[0] == 4
    assert (await invariants(api, ev))["passed"] is True


async def test_no_token_holds_near_the_end_of_the_claim_phase(api, db):
    ev, _ = await drawn_event(api, db, 4, inventory=1, ttl=60)
    db.execute("UPDATE events SET claim_phase_ends_at = fd_now() + interval '90 seconds'")
    advance(db, 61)                                                  # 29 s of phase left: a fresh 60 s hold cannot fit
    res = await sweep(ev)
    assert (res.expired, res.promoted, res.closed) == (1, 0, False)
    assert db.execute("SELECT count(*) FROM allocations WHERE status = 'HELD'").fetchone()[0] == 0


async def test_claim_phase_end_closes_event(api, db):
    ev, _ = await drawn_event(api, db, 6, inventory=2, ttl=60)
    w1, w2 = by_state(db, "WON")
    assert (await claim(api, ev, w1)).status_code == 200
    advance(db, 10_000)                                              # far past claim_phase_ends_at
    res = await sweep(ev)
    assert res.closed is True and res.expired == 1 and res.promoted == 0
    states = dict(db.execute("SELECT state, count(*) FROM entries GROUP BY state").fetchall())
    assert states == {"CLAIMED": 1, "EXPIRED": 1, "LOST": 4}
    assert db.execute("SELECT phase, closed_at IS NOT NULL FROM events").fetchone() == ("CLOSED", True)
    r = await claim(api, ev, w2)
    assert r.status_code == 410 and r.json()["code"] == "HOLD_EXPIRED"
    assert (await sweep(ev)) == lifecycle.SweepResult()              # idempotent once closed
    assert (await invariants(api, ev))["passed"] is True


async def test_concurrent_sweeps_and_claims_never_break_invariants(api, db):
    ev, _ = await drawn_event(api, db, 12, inventory=4, ttl=60)
    winners = by_state(db, "WON")
    advance(db, 59.0)                                                # holds still alive: claims race expiry below
    claims = [claim(api, ev, w, key=f"c-{w}") for w in winners]
    advance(db, 3.0)                                                 # now expired before sweeps start
    results = await asyncio.gather(*claims, *[sweep(ev) for _ in range(6)])
    promoted = sum(r.promoted for r in results[len(winners):])
    inv = await invariants(api, ev)
    assert inv["passed"] is True, inv
    n_claimed = db.execute("SELECT count(*) FROM entries WHERE state = 'CLAIMED'").fetchone()[0]
    n_won = db.execute("SELECT count(*) FROM entries WHERE state = 'WON'").fetchone()[0]
    assert n_claimed + n_won <= 4
    assert n_won == promoted and promoted <= 8


# ------------------------------------------------------------------ worker
async def test_worker_runs_the_whole_lifecycle(api, db):
    users = make_users(db, 6)
    ev = await create_event(api, inventory=2, opens_in=60, closes_in=120, ttl=60)   # opens in 1 min
    async def tick():
        async with get_engine().connect() as conn:
            return dict(await worker.tick(conn))

    assert await tick() == {}                                        # not due yet
    advance(db, 61)
    assert await tick() == {"opened": 1}
    rs = await asyncio.gather(*[api.post(f"/events/{ev['id']}/enter", headers=U(u)) for u in users])
    assert all(r.status_code == 200 for r in rs)
    advance(db, 70)                                                  # past the closing time
    assert (await tick()) == {"drawn": 1}
    assert db.execute("SELECT phase FROM events").fetchone() == ("CLAIMING",)
    w = by_state(db, "WON")[0]
    assert (await claim(api, ev, w)).status_code == 200
    advance(db, 61)                                                  # the other winner's hold lapses
    assert await tick() == {"holds_expired": 1, "promoted": 1}
    advance(db, 10_000)
    assert await tick() == {"holds_expired": 1, "claim_phase_closed": 1}
    assert db.execute("SELECT phase FROM events").fetchone() == ("CLOSED",)
    assert await tick() == {}                                        # nothing left to do
    assert (await invariants(api, ev))["passed"] is True


async def test_worker_waits_for_a_late_beacon_and_closes_fcfs(api, db, monkeypatch):
    from app.services.beacon import MockBeacon

    class Late(MockBeacon):
        published = False

        async def fetch(self, round_):
            return MockBeacon.value_for(round_) if Late.published else None

    monkeypatch.setattr("app.services.draw.get_beacon", lambda: Late())
    ev = await create_event(api, inventory=1, closes_in=30)
    fcfs = await create_event(api, mode="FCFS", closes_in=30)
    advance(db, 31)

    async def tick():
        async with get_engine().connect() as conn:
            return dict(await worker.tick(conn))

    assert await tick() == {"waiting_for_beacon": 1, "closed": 1}
    assert await tick() == {"waiting_for_beacon": 1}                 # still DRAWING, retried each tick
    assert db.execute("SELECT phase FROM events WHERE id = %s", (ev["id"],)).fetchone() == ("DRAWING",)
    assert db.execute("SELECT phase FROM events WHERE id = %s", (fcfs["id"],)).fetchone() == ("CLOSED",)
    Late.published = True
    assert await tick() == {"drawn": 1}


# ------------------------------------------------------------------ stats + invariants
async def test_stats_are_real(api, db):
    ev, users = await drawn_event(api, db, 7, inventory=3)
    w = by_state(db, "WON")[0]
    await claim(api, ev, w)
    s = (await api.get(f"/admin/events/{ev['id']}/stats", headers=ADMIN)).json()
    assert s["by_state"]["CLAIMED"] == 1 and s["by_state"]["WON"] == 2 and s["by_state"]["WAITLISTED"] == 4
    assert s["entrants"] == 7
    assert s["allocations"] == {"inventory": 3, "claimed": 1, "held": 2, "available": 0}
    assert s["holds"] == {"active": 2, "expired": 0}


async def test_invariants_flag_a_dead_worker_then_recover(api, db):
    ev, _ = await drawn_event(api, db, 4, inventory=1, ttl=60)
    assert (await invariants(api, ev))["passed"] is True
    advance(db, 200)                                                 # hold long expired, nothing swept it
    inv = await invariants(api, ev)
    assert inv["passed"] is False and inv["orphaned_holds"] == 1
    assert {c["name"]: c["passed"] for c in inv["checks"]}["no_orphaned_holds"] is False
    await sweep(ev)
    assert (await invariants(api, ev))["passed"] is True


async def test_invariants_for_unknown_event(api, db):
    r = await api.get("/admin/events/00000000-0000-0000-0000-000000000001/invariants", headers=ADMIN)
    assert r.status_code == 404
