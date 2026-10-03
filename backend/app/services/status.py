"""GET /events/{id}/status: a pure, read-only re-read of server state.

Safe across refreshes, reconnects and replicas because nothing about the user
is held anywhere except Postgres. Visibility rules (no pre-draw leaks):
  * public_id, waitlist_position: only once the draw has completed (drawn_at set)
  * hold_expires_at: only while the user is WON and holds a seat
  * seat_no, ticket_code: only once CLAIMED
Weight, risk and draw_rank are never returned.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.errors import ApiError, ErrorCode
from app.schemas import StatusResponse
from app.services.events import window_status

_STATUS = text("""
    SELECT ev.id, ev.phase, ev.window_opens_at, ev.window_closes_at, ev.drawn_at,
           fd_now() AS server_now,
           en.state, en.entered_at, en.public_id, en.waitlist_position,
           al.status AS alloc_status, al.hold_expires_at, al.ticket_code, s.seat_no
      FROM events ev
      LEFT JOIN entries en      ON en.event_id = ev.id AND en.user_id = :uid
      LEFT JOIN allocations al  ON al.entry_id = en.id AND al.status IN ('HELD', 'CONFIRMED')
      LEFT JOIN seats s         ON s.id = al.seat_id
     WHERE ev.id = :eid
""")


async def get_status(conn: AsyncConnection, user_id: UUID, event_id: UUID) -> StatusResponse:
    r = (await conn.execute(_STATUS, {"eid": event_id, "uid": user_id})).first()
    if r is None or r.phase == "DRAFT":
        raise ApiError(ErrorCode.NOT_FOUND, "Event not found")

    now = r.server_now
    out = StatusResponse(event_id=r.id, phase=r.phase,
                         window_status=window_status(r.phase, r.window_opens_at, r.window_closes_at, now),
                         state=r.state or "REGISTERED", entered_at=r.entered_at, server_now=now)
    if r.state is None:
        return out

    drawn = r.drawn_at is not None
    if drawn:
        out.public_id = r.public_id
    if r.state == "WAITLISTED" and drawn:
        out.waitlist_position = r.waitlist_position
    if r.state == "WON" and r.alloc_status == "HELD":
        out.hold_expires_at = r.hold_expires_at
    if r.state == "CLAIMED" and r.alloc_status == "CONFIRMED":
        out.seat_no = r.seat_no
        out.ticket_code = r.ticket_code
    return out

