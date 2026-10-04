"""Run the lottery draw for an event whose entry window has closed.

Three steps, in this order on purpose:

  1. CLOSE  (transaction): lock the event row; if the window's time has passed
     but the phase has not flipped yet, close it (SCHEDULED|OPEN -> DRAWING).
     After this commits no entry can be added (entry insert takes FOR KEY SHARE
     on the same row), so the entrant set is final.
  2. BEACON (no transaction): fetch the beacon round committed at scheduling.
     Done AFTER the close so the randomness cannot influence which entrants
     exist, and outside any transaction because it is a network call. If the
     round is not published yet the phase stays DRAWING and the call can be
     repeated (BEACON_PENDING).
  3. DRAW   (transaction): lock the event row again, re-check nothing else
     drew meanwhile, compute the draw from the entrants as stored, then write
     every outcome atomically: entry ranks/states, one HELD allocation per
     winner (rank k gets seat k, hold expiring claim_ttl_seconds from now), the
     seed reveal, drawn_at and phase CLAIMING. A crash anywhere rolls the whole
     step back, so an event is either not drawn or fully drawn.

Re-running a drawn event is a no-op (changed=False). Two replicas drawing at the
same moment serialise on the row lock; the second one sees drawn_at and stops.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import transaction
from app.errors import ApiError, ErrorCode
from app.services import audit, draw_algo, phases
from app.services.beacon import get_beacon
from app.services.events import EventInfo, load_event

_CHUNK = 10_000   # rows per UPDATE/INSERT statement, to keep statements bounded

_RANK_UPDATE = text("""
    UPDATE entries en
       SET state = v.state, draw_rank = v.rank, waitlist_position = v.wp
      FROM unnest(CAST(:ids AS bigint[]), CAST(:ranks AS integer[]),
                  CAST(:states AS text[]), CAST(:wps AS integer[])) AS v(id, rank, state, wp)
     WHERE en.id = v.id AND en.event_id = :eid
""")

_HOLD_INSERT = text("""
    INSERT INTO allocations (event_id, entry_id, seat_id, status, held_at, hold_expires_at)
    SELECT :eid, v.id, s.id, 'HELD', fd_now(), fd_now() + make_interval(secs => CAST(:ttl AS double precision))
      FROM unnest(CAST(:ids AS bigint[]), CAST(:ranks AS integer[])) AS v(id, rank)
      JOIN seats s ON s.event_id = :eid AND s.seat_no = v.rank
""")


def _chunks(n: int):
    for i in range(0, n, _CHUNK):
        yield slice(i, i + _CHUNK)


async def run_draw(conn: AsyncConnection, event_id: UUID) -> tuple[EventInfo, datetime, bool]:
    """Draw the event. Returns (event, server_now, changed)."""
    # ---- 1. CLOSE
    async with transaction(conn):
        found = await load_event(conn, event_id, for_update=True)
        if found is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Event not found")
        ev, now = found
        if ev.mode != "LOTTERY":
            raise ApiError(ErrorCode.INVALID_PHASE, "Only LOTTERY events have a draw")
        if ev.drawn_at is not None:
            return ev, now, False
        if ev.phase == "DRAFT":
            raise ApiError(ErrorCode.INVALID_PHASE, "Schedule the event first")
        if ev.phase in ("SCHEDULED", "OPEN"):
            if now < ev.window_closes_at:
                raise ApiError(ErrorCode.INVALID_PHASE,
                               "The entry window is still open; close it first (POST .../close)",
                               details={"window_closes_at": ev.window_closes_at.isoformat(),
                                        "server_now": now.isoformat()})
            await phases.close_event_window(conn, event_id, manual=False)
        elif ev.phase != "DRAWING":
            raise ApiError(ErrorCode.INVALID_PHASE, f"Cannot draw an event in phase {ev.phase}")
        assert ev.beacon_round is not None   # guaranteed by events CHECK for scheduled lotteries

    # ---- 2. BEACON (outside any transaction)
    beacon = get_beacon()
    try:
        value = await beacon.fetch(ev.beacon_round)
    except (httpx.HTTPError, ValueError) as exc:
        raise ApiError(ErrorCode.BEACON_PENDING, "The randomness beacon could not be read; retry shortly",
                       details={"beacon_round": ev.beacon_round, "reason": type(exc).__name__}) from exc
    if value is None:
        raise ApiError(ErrorCode.BEACON_PENDING, "The committed beacon round has not been published yet",
                       details={"beacon_round": ev.beacon_round,
                                "available_at": beacon.round_time(ev.beacon_round).isoformat()})

    # ---- 3. DRAW
    async with transaction(conn):
        ev, now = (await load_event(conn, event_id, for_update=True))   # type: ignore[misc]
        if ev.drawn_at is not None:          # another replica finished first
            return ev, now, False
        if ev.phase != "DRAWING":
            raise ApiError(ErrorCode.INVALID_PHASE, f"Cannot draw an event in phase {ev.phase}")

        seed_hex = (await conn.execute(
            text("SELECT server_seed FROM event_secrets WHERE event_id = :id"), {"id": event_id}
        )).scalar_one()
        rows = (await conn.execute(text(
            "SELECT id, public_id::text AS pid, weight FROM entries WHERE event_id = :id AND weight > 0"
        ), {"id": event_id})).all()
        entry_ids = {r.pid: r.id for r in rows}
        entrants = [draw_algo.Entrant(public_id=r.pid, weight=Decimal(r.weight)) for r in rows]

        result = await asyncio.to_thread(
            draw_algo.run_draw, str(event_id), bytes.fromhex(seed_hex),
            bytes.fromhex(value.randomness), entrants, ev.inventory)

        n_win = len(result.winners)
        ids = [entry_ids[p] for p in result.order]
        ranks = list(range(1, len(ids) + 1))
        states = ["WON" if r <= n_win else "WAITLISTED" for r in ranks]
        wps = [None if r <= n_win else r - n_win for r in ranks]
        for s in _chunks(len(ids)):
            await conn.execute(_RANK_UPDATE, {"eid": event_id, "ids": ids[s], "ranks": ranks[s],
                                              "states": states[s], "wps": wps[s]})
        # Entered with weight 0 (flagged by the defence layer): recorded, never drawn.
        await conn.execute(text(
            "UPDATE entries SET state = 'LOST' WHERE event_id = :id AND weight = 0 AND state = 'ENTERED'"
        ), {"id": event_id})

        for s in _chunks(n_win):
            res = await conn.execute(_HOLD_INSERT, {"eid": event_id, "ids": ids[:n_win][s],
                                                    "ranks": ranks[:n_win][s],
                                                    "ttl": ev.claim_ttl_seconds})
            if res.rowcount != len(ids[:n_win][s]):
                raise RuntimeError("draw: a winner had no seat; seats do not match inventory")

        await conn.execute(text("""
            UPDATE events
               SET phase = 'CLAIMING', drawn_at = fd_now(),
                   -- a late draw (beacon wait) must still leave winners a full hold
                   claim_phase_ends_at = GREATEST(claim_phase_ends_at,
                                                  fd_now() + make_interval(secs => claim_ttl_seconds)),
                   beacon_randomness = :rnd, beacon_signature = :sig,
                   entrants_hash = :eh, entrant_count = :n,
                   final_seed = :fs, revealed_seed = :seed, updated_at = now()
             WHERE id = :id AND phase = 'DRAWING'
        """), {"id": event_id, "rnd": value.randomness, "sig": value.signature or None,
               "eh": result.entrants_hash, "n": len(entrants), "fs": result.final_seed, "seed": seed_hex})

        await audit.record(conn, event_id, "draw_completed", {
            "algorithm_version": ev.algorithm_version,
            "beacon_round": value.round, "beacon_randomness": value.randomness,
            "entrants_hash": result.entrants_hash, "entrants_count": len(entrants),
            "final_seed": result.final_seed, "server_seed": seed_hex,
            "winners_count": n_win, "waitlist_count": len(result.waitlist),
            "winners_hash": result.winners_hash, "waitlist_hash": result.waitlist_hash,
        })
        found = await load_event(conn, event_id)
        assert found is not None
        return found[0], found[1], True
