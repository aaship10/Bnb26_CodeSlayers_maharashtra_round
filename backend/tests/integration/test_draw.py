"""Stage 3: the draw end to end through the API, against real Postgres."""
import asyncio
import hashlib
from decimal import Decimal

import pytest

from app.services import draw_algo
from app.services.beacon import MockBeacon
from tests.conftest import ADMIN, create_event, make_users

pytestmark = pytest.mark.db


def U(uid) -> dict:
    return {"X-User-Id": str(uid)}


async def enter_all(api, ev_id, users):
    for u in users:
        assert (await api.post(f"/events/{ev_id}/enter", headers=U(u))).status_code == 200


async def close_and_draw(api, ev_id):
    assert (await api.post(f"/admin/events/{ev_id}/close", headers=ADMIN)).status_code == 200
    return await api.post(f"/admin/events/{ev_id}/draw", headers=ADMIN)


def q(db, sql, *args):
    return db.execute(sql, args).fetchall()


async def test_full_draw_writes_consistent_state(api, db):
    users = make_users(db, 20)
    ev = await create_event(api, inventory=5)
    await enter_all(api, ev["id"], users)
    r = await close_and_draw(api, ev["id"])
    assert r.status_code == 200 and r.json()["changed"] is True
    e = r.json()["event"]
    assert e["phase"] == "CLAIMING" and e["drawn_at"] and e["entrant_count"] == 20

    states = dict(q(db, "SELECT state, count(*) FROM entries GROUP BY state"))
    assert states == {"WON": 5, "WAITLISTED": 15}
    # winners are ranks 1..5 and hold seats 1..5; waitlist positions are 1..15
    held = q(db, """SELECT en.draw_rank, s.seat_no, al.status, al.hold_expires_at > al.held_at
                      FROM allocations al JOIN entries en ON en.id = al.entry_id
                      JOIN seats s ON s.id = al.seat_id ORDER BY en.draw_rank""")
    assert [(a, b, c, d) for a, b, c, d in held] == [(i, i, "HELD", True) for i in range(1, 6)]
    wl = q(db, "SELECT waitlist_position FROM entries WHERE state = 'WAITLISTED' ORDER BY draw_rank")
    assert [w[0] for w in wl] == list(range(1, 16))
    assert db.execute("SELECT count(*) FROM entries WHERE draw_rank IS NULL").fetchone()[0] == 0


async def test_public_data_lets_anyone_reproduce_the_draw(api, db):
    users = make_users(db, 30)
    ev = await create_event(api, inventory=7)
    eid = ev["id"]
    commitment = ev["seed_commitment"]
    await enter_all(api, eid, users)
    await close_and_draw(api, eid)

    f = (await api.get(f"/events/{eid}/fairness")).json()
    ents = (await api.get(f"/events/{eid}/fairness/entrants")).json()
    res = (await api.get(f"/events/{eid}/fairness/results")).json()

    # commitment was published at scheduling; the revealed seed must match it
    assert f["seed_commitment"] == commitment
    assert hashlib.sha256(bytes.fromhex(f["server_seed"])).hexdigest() == commitment
    # beacon value is the committed round's
    assert f["beacon"]["randomness"] == MockBeacon.value_for(f["beacon"]["round"]).randomness

    entrants = [draw_algo.Entrant(x["public_id"], Decimal(str(x["weight"]))) for x in ents["entrants"]]
    assert [x["public_id"] for x in ents["entrants"]] == sorted(x["public_id"] for x in ents["entrants"])
    mine = draw_algo.run_draw(eid, bytes.fromhex(f["server_seed"]),
                              bytes.fromhex(f["beacon"]["randomness"]), entrants, f["inventory"])
    assert mine.entrants_hash == f["entrants_hash"] and mine.final_seed == f["final_seed"]
    assert mine.winners == res["winners"] and mine.waitlist == res["waitlist"]
    assert f["result"] == {"winners_count": 7, "waitlist_count": 23,
                           "winners_hash": mine.winners_hash, "waitlist_hash": mine.waitlist_hash}


async def test_draw_is_idempotent(api, db):
    users = make_users(db, 6)
    ev = await create_event(api, inventory=2)
    await enter_all(api, ev["id"], users)
    await close_and_draw(api, ev["id"])
    before = q(db, "SELECT id, state, draw_rank FROM entries ORDER BY id")
    r = await api.post(f"/admin/events/{ev['id']}/draw", headers=ADMIN)
    assert r.status_code == 200 and r.json()["changed"] is False
    assert q(db, "SELECT id, state, draw_rank FROM entries ORDER BY id") == before
    assert db.execute("SELECT count(*) FROM allocations").fetchone()[0] == 2


async def test_concurrent_draws_run_once(api, db):
    users = make_users(db, 40)
    ev = await create_event(api, inventory=10)
    await enter_all(api, ev["id"], users)
    await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)
    rs = await asyncio.gather(*[api.post(f"/admin/events/{ev['id']}/draw", headers=ADMIN) for _ in range(8)])
    assert all(r.status_code == 200 for r in rs), [r.text for r in rs]
    assert sum(r.json()["changed"] for r in rs) == 1
    assert db.execute("SELECT count(*) FROM allocations").fetchone()[0] == 10


async def test_draw_while_window_open_is_refused(api, db):
    ev = await create_event(api)
    r = await api.post(f"/admin/events/{ev['id']}/draw", headers=ADMIN)
    assert r.status_code == 409 and r.json()["code"] == "INVALID_PHASE"
    assert (await api.get(f"/events/{ev['id']}/fairness/entrants")).status_code == 409
    assert (await api.get(f"/events/{ev['id']}/fairness/results")).status_code == 409
    f = (await api.get(f"/events/{ev['id']}/fairness")).json()
    assert f["server_seed"] is None and f["final_seed"] is None and f["result"] is None


async def test_draw_after_window_time_passes_closes_automatically(api, db):
    users = make_users(db, 4)
    ev = await create_event(api, inventory=2, closes_in=30)
    await enter_all(api, ev["id"], users)
    db.execute("UPDATE dev_clock SET offset_seconds = 31 WHERE id")
    r = await api.post(f"/admin/events/{ev['id']}/draw", headers=ADMIN)
    assert r.status_code == 200 and r.json()["event"]["phase"] == "CLAIMING"


async def test_draft_and_fcfs_cannot_be_drawn(api, db):
    d = await create_event(api, schedule=False)
    r = await api.post(f"/admin/events/{d['id']}/draw", headers=ADMIN)
    assert r.status_code == 409
    f = await create_event(api, mode="FCFS")
    r = await api.post(f"/admin/events/{f['id']}/draw", headers=ADMIN)
    assert r.status_code == 409 and r.json()["code"] == "INVALID_PHASE"
    assert (await api.get(f"/events/{f['id']}/fairness")).status_code == 404


async def test_beacon_not_published_leaves_event_drawing_then_retry_succeeds(api, db, monkeypatch):
    users = make_users(db, 5)
    ev = await create_event(api, inventory=2)
    await enter_all(api, ev["id"], users)

    class Late(MockBeacon):
        published = False

        async def fetch(self, round_):
            return MockBeacon.value_for(round_) if Late.published else None

    monkeypatch.setattr("app.services.draw.get_beacon", lambda: Late())
    r = await close_and_draw(api, ev["id"])
    assert r.status_code == 409 and r.json()["code"] == "BEACON_PENDING"
    assert r.json()["details"]["beacon_round"] == ev["beacon_round"]
    row = q(db, "SELECT phase, drawn_at FROM events")[0]
    assert row == ("DRAWING", None)
    assert db.execute("SELECT count(*) FROM entries WHERE draw_rank IS NOT NULL").fetchone()[0] == 0

    Late.published = True
    r = await api.post(f"/admin/events/{ev['id']}/draw", headers=ADMIN)
    assert r.status_code == 200 and r.json()["changed"] is True


async def test_zero_entrants_and_fewer_than_seats(api, db):
    empty = await create_event(api, inventory=3)
    r = await close_and_draw(api, empty["id"])
    assert r.status_code == 200 and r.json()["event"]["entrant_count"] == 0
    f = (await api.get(f"/events/{empty['id']}/fairness")).json()
    assert f["result"]["winners_count"] == 0

    users = make_users(db, 3)
    ev = await create_event(api, inventory=10)
    await enter_all(api, ev["id"], users)
    await close_and_draw(api, ev["id"])
    assert dict(q(db, "SELECT state, count(*) FROM entries WHERE event_id = %s GROUP BY state",
                  ev["id"])) == {"WON": 3}


async def test_weights_zero_excluded_and_reduced_weights_in_hash(api, db):
    users = make_users(db, 8)
    ev = await create_event(api, inventory=3)
    await enter_all(api, ev["id"], users)
    db.execute("UPDATE entries SET weight = 0 WHERE user_id = %s AND event_id = %s", (users[0], ev["id"]))
    db.execute("UPDATE entries SET weight = 0.5 WHERE user_id = %s AND event_id = %s", (users[1], ev["id"]))
    db.execute("UPDATE entries SET weight = 0.25 WHERE user_id = %s AND event_id = %s", (users[2], ev["id"]))
    await close_and_draw(api, ev["id"])
    assert q(db, "SELECT state, draw_rank FROM entries WHERE user_id = %s", users[0]) == [("LOST", None)]
    ents = (await api.get(f"/events/{ev['id']}/fairness/entrants")).json()
    assert ents["count"] == 7 and sorted({e["weight"] for e in ents["entrants"]}) == [0.25, 0.5, 1.0]
    f = (await api.get(f"/events/{ev['id']}/fairness")).json()
    entrants = [draw_algo.Entrant(x["public_id"], Decimal(str(x["weight"]))) for x in ents["entrants"]]
    mine = draw_algo.run_draw(ev["id"], bytes.fromhex(f["server_seed"]),
                              bytes.fromhex(f["beacon"]["randomness"]), entrants, 3)
    assert mine.final_seed == f["final_seed"]


async def test_status_reflects_draw_without_leaking_rank(api, db):
    users = make_users(db, 10)
    ev = await create_event(api, inventory=3)
    await enter_all(api, ev["id"], users)
    await close_and_draw(api, ev["id"])
    seen = {"WON": 0, "WAITLISTED": 0}
    for u in users:
        s = (await api.get(f"/events/{ev['id']}/status", headers=U(u))).json()
        seen[s["state"]] += 1
        assert s["public_id"] and "draw_rank" not in s and "weight" not in s
        if s["state"] == "WON":
            assert s["hold_expires_at"] and "seat_no" not in s
        else:
            assert s["waitlist_position"] >= 1
    assert seen == {"WON": 3, "WAITLISTED": 7}


async def test_seed_not_public_before_draw_and_fairness_hides_secrets(api, db):
    users = make_users(db, 3)
    ev = await create_event(api, inventory=1)
    await enter_all(api, ev["id"], users)
    await api.post(f"/admin/events/{ev['id']}/close", headers=ADMIN)
    f = (await api.get(f"/events/{ev['id']}/fairness")).json()     # DRAWING, not drawn
    assert f["server_seed"] is None and f["final_seed"] is None
    seed = q(db, "SELECT server_seed FROM event_secrets")[0][0]
    assert seed not in (await api.get(f"/events/{ev['id']}/fairness")).text
    assert seed not in (await api.get(f"/events/{ev['id']}")).text
    # the entrant list is available once closed, and carries no user ids
    ents = (await api.get(f"/events/{ev['id']}/fairness/entrants")).text
    assert not any(str(u) in ents for u in users)
