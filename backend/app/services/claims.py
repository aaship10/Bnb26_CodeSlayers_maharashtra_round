"""POST /events/{id}/claim: turn a held seat into a confirmed ticket.

One conditional UPDATE decides the race between the user and the expiry sweeper:

    UPDATE allocations SET status = 'CONFIRMED' ...
     WHERE id = :a AND status = 'HELD' AND hold_expires_at > fd_now()

Both sides lock the allocation row, so exactly one of "confirmed" or "expired"
wins; the loser re-evaluates the WHERE on the new row version and matches
nothing. The seat itself can never be double-allocated regardless of what this
code does: the partial unique indexes on allocations forbid it.

Errors (codes are the public contract):
  NOT_WINNER (403)       no entry, not drawn, lost the draw, or on the waitlist
  HOLD_EXPIRED (410)     had a hold and it ran out (or the claim phase ended)
  ALREADY_CLAIMED (409)  already holds a confirmed ticket (a repeat with the SAME
                         Idempotency-Key is replayed as the original 200 instead)
  INVALID_PHASE (409)    the draw has not finished yet
"""
from __future__ import annotations

import secrets
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.errors import ApiError, ErrorCode
from app.schemas import ClaimResponse
from app.services import audit

_READ = text("""
    SELECT ev.phase, ev.mode, ev.claim_phase_ends_at, fd_now() AS server_now,
           en.id AS entry_id, en.state, en.public_id,
           al.id AS alloc_id, al.status AS alloc_status, s.seat_no
      FROM events ev
      LEFT JOIN entries en     ON en.event_id = ev.id AND en.user_id = :uid
      LEFT JOIN allocations al ON al.entry_id = en.id AND al.status IN ('HELD', 'CONFIRMED')
      LEFT JOIN seats s        ON s.id = al.seat_id
     WHERE ev.id = :eid
""")

_CONFIRM = text("""
    UPDATE allocations
       SET status = 'CONFIRMED', confirmed_at = fd_now(), ticket_code = :code
     WHERE id = :aid AND status = 'HELD' AND hold_expires_at > fd_now()
    RETURNING seat_id
""")


def new_ticket_code() -> str:
    h = secrets.token_hex(8).upper()
    return "FD-" + "-".join(h[i:i + 4] for i in range(0, 16, 4))


async def claim(conn: AsyncConnection, user_id: UUID, event_id: UUID) -> ClaimResponse:
    """Run inside the caller's transaction."""
    r = (await conn.execute(_READ, {"eid": event_id, "uid": user_id})).first()
    if r is None or r.phase == "DRAFT":
        raise ApiError(ErrorCode.NOT_FOUND, "Event not found")
    if r.mode != "LOTTERY":
        raise ApiError(ErrorCode.INVALID_PHASE, "Claiming applies to lottery events", status=501)
    now = r.server_now

    if r.state == "CLAIMED":
        raise ApiError(ErrorCode.ALREADY_CLAIMED, "You have already claimed a seat")
    if r.state == "EXPIRED":
        raise ApiError(ErrorCode.HOLD_EXPIRED, "Your hold has expired")
    if r.phase in ("SCHEDULED", "OPEN", "DRAWING"):
        raise ApiError(ErrorCode.INVALID_PHASE, "The draw has not finished yet")
    if r.state != "WON" or r.alloc_status != "HELD":
        raise ApiError(ErrorCode.NOT_WINNER, "No seat is being held for you")
    if r.phase != "CLAIMING" or (r.claim_phase_ends_at is not None and now >= r.claim_phase_ends_at):
        raise ApiError(ErrorCode.HOLD_EXPIRED, "The claim phase has ended")

    code = new_ticket_code()
    confirmed = (await conn.execute(_CONFIRM, {"aid": r.alloc_id, "code": code})).first()
    if confirmed is None:
        # The conditional UPDATE matched nothing: someone changed the allocation after
        # our read. Say which (a concurrent claim of our own vs. expiry).
        now_status = (await conn.execute(
            text("SELECT status FROM allocations WHERE id = :aid"), {"aid": r.alloc_id})).scalar_one()
        if now_status == "CONFIRMED":
            raise ApiError(ErrorCode.ALREADY_CLAIMED, "You have already claimed a seat")
        raise ApiError(ErrorCode.HOLD_EXPIRED, "Your hold has expired")
    moved = await conn.execute(
        text("UPDATE entries SET state = 'CLAIMED' WHERE id = :id AND state = 'WON'"), {"id": r.entry_id})
    if moved.rowcount != 1:
        raise RuntimeError("claim: allocation confirmed but entry was not WON")
    await audit.record(conn, event_id, "seat_claimed", {"public_id": str(r.public_id), "seat_no": r.seat_no})
    return ClaimResponse(seat_no=r.seat_no, ticket_code=code, server_now=now)
