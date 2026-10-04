"""Operational read models for organisers and for Member B's chaos harness.

`stats` summarises where an event is; `invariants` re-derives from the tables the
properties the system must never break. The database constraints already make
most of these impossible, so this is an independent check that they hold, run
after load tests, chaos runs and demos.
"""
from __future__ import annotations

from datetime import datetime, timezone
try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.errors import ApiError, ErrorCode
from app.services.events import load_event

# A hold this far past its expiry that is still HELD means the worker is not running.
STALE_HOLD_GRACE_SECONDS = 30

ENTRY_STATES = ("ENTERED", "WON", "WAITLISTED", "CLAIMED", "EXPIRED", "LOST")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def stats(conn: AsyncConnection, event_id: UUID) -> dict[str, Any]:
    found = await load_event(conn, event_id)
    if found is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Event not found")
    ev, now = found
    by_state = dict.fromkeys(ENTRY_STATES, 0)
    for r in (await conn.execute(text(
            "SELECT state, count(*) AS n FROM entries WHERE event_id = :id GROUP BY state"), {"id": event_id})):
        by_state[r.state] = r.n
    entrants = sum(by_state.values())
    a = (await conn.execute(text("""
        SELECT count(*) FILTER (WHERE status = 'CONFIRMED') AS claimed,
               count(*) FILTER (WHERE status = 'HELD')      AS held,
               count(*) FILTER (WHERE status = 'HELD' AND hold_expires_at > fd_now()) AS live,
               count(*) FILTER (WHERE status = 'EXPIRED')   AS expired
          FROM allocations WHERE event_id = :id"""), {"id": event_id})).one()
    registered = (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one() - entrants
    return {
        "event_id": str(event_id), "phase": ev.phase,
        "by_state": {"REGISTERED": max(registered, 0), **by_state},
        "entrants": entrants,
        "allocations": {"inventory": ev.inventory, "claimed": a.claimed, "held": a.held,
                        "available": ev.inventory - a.claimed - a.held},
        "holds": {"active": a.live, "expired": a.expired},
        "server_now": _iso(now),
    }


async def invariants(conn: AsyncConnection, event_id: UUID) -> dict[str, Any]:
    found = await load_event(conn, event_id)
    if found is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Event not found")
    ev, now = found
    p = {"id": event_id, "grace": STALE_HOLD_GRACE_SECONDS}

    async def scalar(sql: str) -> int:
        return (await conn.execute(text(sql), p)).scalar_one()

    active = "event_id = :id AND status IN ('HELD', 'CONFIRMED')"
    oversold = max(0, await scalar(f"SELECT count(*) FROM allocations WHERE {active}") - ev.inventory)
    dup_seats = await scalar(
        f"SELECT count(*) FROM (SELECT seat_id FROM allocations WHERE {active} "
        "GROUP BY seat_id HAVING count(*) > 1) t")
    dup_users = await scalar(
        "SELECT count(*) FROM (SELECT en.user_id FROM allocations al JOIN entries en ON en.id = al.entry_id "
        "WHERE al.event_id = :id AND al.status IN ('HELD', 'CONFIRMED') "
        "GROUP BY en.user_id HAVING count(*) > 1) t")
    # A hold with no live claim on it: its entry is not WON, the event is over, or the
    # hold is long expired and nothing swept it.
    orphaned = await scalar(
        "SELECT count(*) FROM allocations al JOIN entries en ON en.id = al.entry_id "
        "JOIN events ev ON ev.id = al.event_id "
        "WHERE al.event_id = :id AND al.status = 'HELD' AND "
        "(en.state <> 'WON' OR ev.phase = 'CLOSED' "
        " OR al.hold_expires_at < fd_now() - make_interval(secs => CAST(:grace AS double precision)))")
    # Entry state and allocation must agree: CLAIMED <=> exactly one CONFIRMED, WON <=> one HELD.
    mismatched = await scalar(
        "SELECT count(*) FROM entries en WHERE en.event_id = :id AND "
        "(SELECT count(*) FROM allocations al WHERE al.entry_id = en.id AND al.status = 'CONFIRMED') "
        "<> (en.state = 'CLAIMED')::int")
    mismatched += await scalar(
        "SELECT count(*) FROM entries en WHERE en.event_id = :id AND en.state = 'WON' AND "
        "(SELECT count(*) FROM allocations al WHERE al.entry_id = en.id AND al.status = 'HELD') <> 1")

    checks = [
        {"name": "no_oversell", "passed": oversold == 0, "count": oversold},
        {"name": "one_seat_one_holder", "passed": dup_seats == 0, "count": dup_seats},
        {"name": "one_seat_per_user", "passed": dup_users == 0, "count": dup_users},
        {"name": "no_orphaned_holds", "passed": orphaned == 0, "count": orphaned},
        {"name": "entry_state_matches_allocation", "passed": mismatched == 0, "count": mismatched},
    ]
    return {
        "oversold": oversold, "duplicate_users": dup_users, "duplicate_seats": dup_seats,
        "orphaned_holds": orphaned, "passed": all(c["passed"] for c in checks), "checks": checks,
        "checked_at": _iso(now), "server_now": _iso(now),
    }
