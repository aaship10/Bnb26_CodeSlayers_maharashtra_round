"""Public data for verifying a draw. Reads `events` and `entries` only (never
`event_secrets`); the server seed is visible solely through events.revealed_seed,
which is written in the same transaction as the draw.

Nothing here exposes user ids, emails, weights tied to a person, or risk data:
entrants are identified by their pseudonymous public_id.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.errors import ApiError, ErrorCode
from app.schemas import (FairnessBeacon, FairnessEntrant, FairnessEntrants, FairnessOutcome,
                         FairnessResponse, FairnessResults)
from app.services import draw_algo
from app.services.beacon import get_beacon
from app.services.events import EventInfo, load_event


async def _lottery(conn: AsyncConnection, event_id: UUID):
    found = await load_event(conn, event_id)
    if found is None or found[0].phase == "DRAFT":
        raise ApiError(ErrorCode.NOT_FOUND, "Event not found")
    ev, now = found
    if ev.mode != "LOTTERY":
        raise ApiError(ErrorCode.NOT_FOUND, "FCFS events have no draw to verify")
    return ev, now


async def _ordered_ids(conn: AsyncConnection, ev: EventInfo) -> tuple[list[str], list[str]]:
    """(winners, waitlist) public ids in draw order, from the immutable draw_rank."""
    rows = (await conn.execute(text(
        "SELECT public_id::text AS pid, draw_rank FROM entries "
        "WHERE event_id = :id AND draw_rank IS NOT NULL ORDER BY draw_rank"), {"id": ev.id})).all()
    n = ev.inventory
    return [r.pid for r in rows if r.draw_rank <= n], [r.pid for r in rows if r.draw_rank > n]


async def get_fairness(conn: AsyncConnection, event_id: UUID) -> FairnessResponse:
    ev, now = await _lottery(conn, event_id)
    drawn = ev.drawn_at is not None
    result = None
    if drawn:
        winners, waitlist = await _ordered_ids(conn, ev)
        result = FairnessOutcome(winners_count=len(winners), waitlist_count=len(waitlist),
                                 winners_hash=draw_algo.list_hash(winners),
                                 waitlist_hash=draw_algo.list_hash(waitlist))
    assert ev.seed_commitment is not None and ev.beacon_round is not None
    return FairnessResponse(
        event_id=ev.id, phase=ev.phase, algorithm_version=ev.algorithm_version,
        seed_commitment=ev.seed_commitment, server_seed=ev.revealed_seed,
        beacon=FairnessBeacon(source=get_beacon().name, round=ev.beacon_round,
                              randomness=ev.beacon_randomness),
        entrants_hash=ev.entrants_hash, entrants_count=ev.entrant_count,
        final_seed=ev.final_seed, inventory=ev.inventory, result=result,
        audit_head_hash=None,   # stage 6: head of the hash-chained audit log
        server_now=now,
    )


async def get_entrants(conn: AsyncConnection, event_id: UUID) -> FairnessEntrants:
    """The exact list the entrants_hash covers, in canonical (public_id) order.
    Not available while the window is open: it would reveal who has entered."""
    ev, now = await _lottery(conn, event_id)
    if ev.window_status(now) != "CLOSED":
        raise ApiError(ErrorCode.INVALID_PHASE, "The entrant list is published when the window closes")
    rows = (await conn.execute(text(
        "SELECT public_id::text AS pid, weight FROM entries WHERE event_id = :id AND weight > 0"),
        {"id": event_id})).all()
    rows = sorted(rows, key=lambda r: r.pid)       # code point order, not the DB collation
    return FairnessEntrants(
        event_id=ev.id, count=len(rows),
        entrants=[FairnessEntrant(public_id=r.pid, weight=float(r.weight)) for r in rows])


async def get_results(conn: AsyncConnection, event_id: UUID) -> FairnessResults:
    ev, _ = await _lottery(conn, event_id)
    if ev.drawn_at is None:
        raise ApiError(ErrorCode.INVALID_PHASE, "The draw has not run yet")
    winners, waitlist = await _ordered_ids(conn, ev)
    return FairnessResults(event_id=ev.id, winners=winners, waitlist=waitlist)
