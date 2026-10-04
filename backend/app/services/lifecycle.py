"""Time-driven transitions after the draw: hold expiry, waitlist promotion and the
end of the claim phase. Called by the worker (and directly by tests).

`sweep_event` is one transaction per event, serialised by the event row lock, so
any number of workers can run it concurrently: the second one waits, then finds
nothing left to do. It does, in order:

  1. EXPIRE   every HELD allocation whose hold has run out (all of them once the
              claim phase has ended); its entry WON -> EXPIRED.
  2. END      if the claim phase is over: waitlisted entries -> LOST, event CLOSED.
  3. PROMOTE  otherwise, give each free seat to the next waitlisted entrant (by
              waitlist_position) as a fresh HELD allocation with a full
              claim_ttl_seconds. Free seats come from expired holds and from
              released seats alike. No promotion if the new hold could not
              finish before the claim phase ends, so nobody gets a token hold.

It never touches CLAIMED entries or CONFIRMED allocations.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import transaction
from app.services import audit
from app.services.events import load_event


@dataclass(frozen=True)
class SweepResult:
    expired: int = 0
    promoted: int = 0
    closed: bool = False


_EXPIRE = text("""
    UPDATE allocations
       SET status = 'EXPIRED', ended_at = fd_now()
     WHERE event_id = :eid AND status = 'HELD' AND (:ended OR hold_expires_at <= fd_now())
    RETURNING entry_id
""")

_PROMOTE_SEATS = text("""
    SELECT s.id, s.seat_no FROM seats s
     WHERE s.event_id = :eid
       AND NOT EXISTS (SELECT 1 FROM allocations a
                        WHERE a.seat_id = s.id AND a.status IN ('HELD', 'CONFIRMED'))
     ORDER BY s.seat_no LIMIT :n
""")

_HOLD = text("""
    INSERT INTO allocations (event_id, entry_id, seat_id, status, held_at, hold_expires_at)
    SELECT :eid, v.entry_id, v.seat_id, 'HELD', fd_now(),
           fd_now() + make_interval(secs => CAST(:ttl AS double precision))
      FROM unnest(CAST(:entries AS bigint[]), CAST(:seats AS bigint[])) AS v(entry_id, seat_id)
""")


async def sweep_event(conn: AsyncConnection, event_id: UUID) -> SweepResult:
    async with transaction(conn):
        found = await load_event(conn, event_id, for_update=True)
        if found is None or found[0].phase != "CLAIMING":
            return SweepResult()
        ev, now = found
        ended = ev.claim_phase_ends_at is not None and now >= ev.claim_phase_ends_at

        expired_entries = [r.entry_id for r in (await conn.execute(
            _EXPIRE, {"eid": event_id, "ended": ended})).all()]
        if expired_entries:
            await conn.execute(text("UPDATE entries SET state = 'EXPIRED' WHERE id = ANY(:ids) AND state = 'WON'"),
                               {"ids": expired_entries})

        if ended:
            await conn.execute(text(
                "UPDATE entries SET state = 'LOST' WHERE event_id = :eid AND state = 'WAITLISTED'"),
                {"eid": event_id})
            await conn.execute(text(
                "UPDATE events SET phase = 'CLOSED', closed_at = fd_now(), updated_at = now() "
                "WHERE id = :eid AND phase = 'CLAIMING'"), {"eid": event_id})
            await audit.record(conn, event_id, "claim_phase_closed", {"holds_expired": len(expired_entries)})
            return SweepResult(expired=len(expired_entries), closed=True)

        promoted: list[str] = []
        room = ev.claim_phase_ends_at is None or (
            (await conn.execute(text("SELECT fd_now() + make_interval(secs => CAST(:t AS double precision)) <= :end"),
                                {"t": ev.claim_ttl_seconds, "end": ev.claim_phase_ends_at})).scalar_one())
        if room:
            active = (await conn.execute(text(
                "SELECT count(*) FROM allocations WHERE event_id = :eid AND status IN ('HELD', 'CONFIRMED')"),
                {"eid": event_id})).scalar_one()
            free = ev.inventory - active
            if free > 0:
                waiters = (await conn.execute(text(
                    "SELECT id, public_id::text AS pid FROM entries "
                    "WHERE event_id = :eid AND state = 'WAITLISTED' "
                    "ORDER BY waitlist_position LIMIT :n FOR UPDATE"), {"eid": event_id, "n": free})).all()
                if waiters:
                    seats = (await conn.execute(_PROMOTE_SEATS, {"eid": event_id, "n": len(waiters)})).all()
                    n = min(len(waiters), len(seats))
                    waiters = waiters[:n]
                    ids = [w.id for w in waiters]
                    await conn.execute(text("UPDATE entries SET state = 'WON' WHERE id = ANY(:ids)"), {"ids": ids})
                    await conn.execute(_HOLD, {"eid": event_id, "ttl": ev.claim_ttl_seconds,
                                               "entries": ids, "seats": [s.id for s in seats[:n]]})
                    promoted = [w.pid for w in waiters]

        if expired_entries:
            await audit.record(conn, event_id, "holds_expired", {"count": len(expired_entries)})
        if promoted:
            await audit.record(conn, event_id, "waitlist_promoted", {"count": len(promoted), "public_ids": promoted})
        return SweepResult(expired=len(expired_entries), promoted=len(promoted))
