"""Attendee endpoints."""
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth import CurrentUser, get_current_user
from app.db import get_conn, transaction
from app.hooks import rate_limit
from app.errors import ApiError, ErrorCode
from app.idempotency import run_idempotent
from app.routers import ERRORS
from app.schemas import ClaimResponse, EnterResponse, EventList, EventPublic, StatusResponse
from app.services import claims as claims_service
from app.services import entry as entry_service
from app.services import status as status_service
from app.services.events import list_events, load_event

router = APIRouter(prefix="/events", tags=["events"], responses=ERRORS)


@router.get("", response_model=EventList)
async def get_events(conn: AsyncConnection = Depends(get_conn)) -> EventList:
    """All non-DRAFT events with server_now for countdown sync."""
    events, now = await list_events(conn, include_draft=False)
    return EventList(events=[EventPublic.build(e, now) for e in events], server_now=now)


@router.get("/{event_id}", response_model=EventPublic)
async def get_event(event_id: UUID, conn: AsyncConnection = Depends(get_conn)) -> EventPublic:
    found = await load_event(conn, event_id)
    if found is None or found[0].phase == "DRAFT":
        raise ApiError(ErrorCode.NOT_FOUND, "Event not found")
    return EventPublic.build(*found)


@router.post("/{event_id}/enter", response_model=EnterResponse, dependencies=[Depends(rate_limit("enter"))])
async def enter_event(event_id: UUID, request: Request,
                      user: CurrentUser = Depends(get_current_user),
                      conn: AsyncConnection = Depends(get_conn)) -> EnterResponse:
    """Enter the draw. Idempotent per identity: repeat calls return the existing
    entry (already_entered=true) and perform no write. When in the window you
    enter makes no difference to your chances.

    Errors: WINDOW_NOT_OPEN, WINDOW_CLOSED (409), CHALLENGE_REQUIRED, REJECTED (403)."""
    return await entry_service.enter(conn, request, user, event_id)


@router.get("/{event_id}/status", response_model=StatusResponse, response_model_exclude_none=True,
            dependencies=[Depends(rate_limit("status"))])
async def get_status(event_id: UUID, user: CurrentUser = Depends(get_current_user),
                     conn: AsyncConnection = Depends(get_conn)) -> StatusResponse:
    """Your current state. Pure read; poll it after refresh/reconnect."""
    return await status_service.get_status(conn, user.id, event_id)


@router.post("/{event_id}/claim", response_model=ClaimResponse, dependencies=[Depends(rate_limit("claim"))])
async def claim_seat(event_id: UUID, user: CurrentUser = Depends(get_current_user),
                     conn: AsyncConnection = Depends(get_conn),
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Confirm the seat held for you. Send an Idempotency-Key and reuse it on retries:
    a repeat with the same key returns the original response (header
    `Idempotent-Replayed: true`) and never issues a second ticket.

    Errors: NOT_WINNER (403), HOLD_EXPIRED (410), ALREADY_CLAIMED (409), INVALID_PHASE (409)."""
    async def op():
        out = await claims_service.claim(conn, user.id, event_id)
        return 200, out.model_dump(mode="json")

    async with transaction(conn):
        status, body, replayed = await run_idempotent(
            conn, key=idempotency_key, principal=user.id, endpoint=f"POST /events/{event_id}/claim",
            payload={}, op=op)
    return JSONResponse(status_code=status, content=body,
                        headers={"Idempotent-Replayed": "true"} if replayed else None)
