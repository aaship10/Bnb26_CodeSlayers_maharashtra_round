"""POST /events/{id}/enter: one entry per identity, server-side window only.

Flow (LOTTERY mode):
  1. Read the event, server time and any existing entry in ONE query.
     Already entered -> return the stored state, write NOTHING. Under a flood of
     duplicates this path is read-only, so it never contends on a hot row.
  2. Window / phase check against server time (clear error codes).
  3. Call Member B's entry_gate OUTSIDE any transaction (it may be slow; we must
     not hold locks or an open transaction while it runs).
  4. Guarded insert, one statement:
       - the window predicate is re-evaluated in the database at insert time,
         so a request that passed step 2 just before the window closed cannot
         sneak in late;
       - FOR KEY SHARE on the event row: closing the window takes FOR UPDATE on
         the same row, which waits for in-flight inserts and makes later ones
         re-check the (now changed) phase, so the draw never misses an entry
         that was committed "during" the close;
       - ON CONFLICT (event_id, user_id) DO NOTHING: concurrent first-time
         requests from one identity create exactly one row (the unique
         constraint decides, not application code).
  5. If nothing was inserted, re-read to report either the existing entry
     (lost a race with our own duplicate) or the precise window error.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app import hooks
from app.auth import CurrentUser
from app.db import transaction
from app.errors import ApiError, ErrorCode
from app.schemas import EnterResponse
from app.services.events import EVENT_COLUMNS, EventInfo

log = logging.getLogger("fairdrop.entry")

_READ = text(f"""
    SELECT {EVENT_COLUMNS}, fd_now() AS server_now,
           en.state AS entry_state, en.entered_at AS entry_entered_at
      FROM events ev
      LEFT JOIN entries en ON en.event_id = ev.id AND en.user_id = :uid
     WHERE ev.id = :eid
""")

_INSERT = text("""
    WITH ev AS (
        SELECT id FROM events
         WHERE id = :eid
           AND mode = 'LOTTERY'
           AND phase IN ('SCHEDULED', 'OPEN')
           AND fd_now() >= window_opens_at
           AND fd_now() <  window_closes_at
           FOR KEY SHARE
    )
    INSERT INTO entries (event_id, user_id, weight, risk)
    SELECT ev.id, :uid, :weight, CAST(:risk AS jsonb) FROM ev
    ON CONFLICT (event_id, user_id) DO NOTHING
    RETURNING state, entered_at, fd_now() AS server_now
""")


def window_error(ev: EventInfo, now: datetime) -> ApiError | None:
    details = {"window_opens_at": ev.window_opens_at.isoformat(),
               "window_closes_at": ev.window_closes_at.isoformat(),
               "server_now": now.isoformat()}
    if ev.phase in ("DRAWING", "CLAIMING", "CLOSED") or now >= ev.window_closes_at:
        return ApiError(ErrorCode.WINDOW_CLOSED, "The entry window has closed", details=details)
    if now < ev.window_opens_at:
        return ApiError(ErrorCode.WINDOW_NOT_OPEN, "The entry window has not opened yet", details=details)
    return None


async def _read(conn: AsyncConnection, event_id: UUID, user_id: UUID):
    row = (await conn.execute(_READ, {"eid": event_id, "uid": user_id})).first()
    if row is None or row.phase == "DRAFT":
        raise ApiError(ErrorCode.NOT_FOUND, "Event not found")
    return row


def _already(row) -> EnterResponse:
    return EnterResponse(state=row.entry_state, entered_at=row.entry_entered_at,
                         already_entered=True, server_now=row.server_now)


async def enter(conn: AsyncConnection, request: Request, user: CurrentUser, event_id: UUID) -> EnterResponse:
    row = await _read(conn, event_id, user.id)
    if row.entry_state is not None:
        return _already(row)                     # fast path: no write, no gate
    ev, now = EventInfo.from_row(row), row.server_now
    if (err := window_error(ev, now)) is not None:
        raise err
    if ev.mode == "FCFS":
        # FCFS baseline entry (immediate seat grab) arrives in stage 5.
        raise ApiError(ErrorCode.INVALID_PHASE, "FCFS entry is not implemented yet", status=501)

    # End the read transaction before the (possibly slow) gate runs.
    if conn.in_transaction():
        await conn.commit()

    decision = await hooks.entry_gate(hooks.GateContext(
        user=user, event=ev, request=request, server_now=now, already_entered=False))
    if decision.action == hooks.GateAction.CHALLENGE:
        raise ApiError(ErrorCode.CHALLENGE_REQUIRED, "Complete the challenge and retry",
                       details={"challenge": decision.challenge})
    if decision.action == hooks.GateAction.REJECT:
        log.info("entry rejected by gate: user=%s event=%s reason=%s", user.id, event_id, decision.reason)
        raise ApiError(ErrorCode.REJECTED, "Entry rejected")

    async with transaction(conn):
        inserted = (await conn.execute(_INSERT, {
            "eid": event_id, "uid": user.id, "weight": decision.weight,
            "risk": json.dumps(decision.risk),
        })).first()
    if inserted is not None:
        return EnterResponse(state=inserted.state, entered_at=inserted.entered_at,
                             already_entered=False, server_now=inserted.server_now)

    # Nothing inserted: a concurrent duplicate won, or the window closed meanwhile.
    row = await _read(conn, event_id, user.id)
    if row.entry_state is not None:
        return _already(row)
    err = window_error(EventInfo.from_row(row), row.server_now)
    raise err or ApiError(ErrorCode.WINDOW_CLOSED, "The entry window has closed")
