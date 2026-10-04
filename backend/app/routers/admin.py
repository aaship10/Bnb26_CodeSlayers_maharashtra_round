"""Admin endpoints (X-Admin-Token). Writes accept an optional Idempotency-Key;
a replay returns the stored response with header `Idempotent-Replayed: true`."""
from collections.abc import Awaitable, Callable
from datetime import timezone
try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth import require_admin
from app.db import get_conn, transaction
from app.errors import ApiError, ErrorCode
from app.idempotency import ADMIN_PRINCIPAL, run_idempotent
from app.routers import ERRORS
from app.schemas import EventAdmin, EventCreate, EventPatch, TransitionResult
from app.services import admin_stats
from app.services import draw as draw_service
from app.services import phases
from app.services.events import list_events, load_event

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)], responses=ERRORS)

IdemKey = Header(default=None, alias="Idempotency-Key")

DEFENCE_PRESETS = [
    {
        "id": "none",
        "name": "None",
        "description": "No defences. Use it to show what an unprotected drop looks like.",
        "defences": {
            "preset": "none",
            "layers": {
                "rate_limit": {"enabled": False, "per_ip_rps": 5, "per_user_rps": 2, "burst": 10},
                "pow": {"enabled": False, "difficulty_bits": 18},
                "captcha": {"enabled": False, "provider": "mock", "when_risk_at_least": 0.7},
                "signals": {"enabled": False, "device_id": True, "honeypot": True, "timing": True},
                "risk": {"enabled": False, "challenge_at": 0.5, "reject_at": 0.9},
            },
        },
    },
    {
        "id": "rate_limit",
        "name": "Rate limit",
        "description": "Per-IP and per-account request caps.",
        "defences": {
            "preset": "rate_limit",
            "layers": {
                "rate_limit": {"enabled": True, "per_ip_rps": 5, "per_user_rps": 2, "burst": 10},
                "pow": {"enabled": False, "difficulty_bits": 18},
                "captcha": {"enabled": False, "provider": "mock", "when_risk_at_least": 0.7},
                "signals": {"enabled": False, "device_id": True, "honeypot": True, "timing": True},
                "risk": {"enabled": False, "challenge_at": 0.5, "reject_at": 0.9},
            },
        },
    },
    {
        "id": "rate_limit+pow",
        "name": "Rate limit + proof-of-work",
        "description": "Each entry costs a little CPU; cheap for a person, costly at bot scale.",
        "defences": {
            "preset": "rate_limit+pow",
            "layers": {
                "rate_limit": {"enabled": True, "per_ip_rps": 5, "per_user_rps": 2, "burst": 10},
                "pow": {"enabled": True, "difficulty_bits": 18},
                "captcha": {"enabled": False, "provider": "mock", "when_risk_at_least": 0.7},
                "signals": {"enabled": False, "device_id": True, "honeypot": True, "timing": True},
                "risk": {"enabled": False, "challenge_at": 0.5, "reject_at": 0.9},
            },
        },
    },
    {
        "id": "rate_limit+pow+captcha",
        "name": "Rate limit + PoW + CAPTCHA",
        "description": "Adds a human check for risky clients.",
        "defences": {
            "preset": "rate_limit+pow+captcha",
            "layers": {
                "rate_limit": {"enabled": True, "per_ip_rps": 5, "per_user_rps": 2, "burst": 10},
                "pow": {"enabled": True, "difficulty_bits": 18},
                "captcha": {"enabled": True, "provider": "mock", "when_risk_at_least": 0.7},
                "signals": {"enabled": False, "device_id": True, "honeypot": True, "timing": True},
                "risk": {"enabled": False, "challenge_at": 0.5, "reject_at": 0.9},
            },
        },
    },
    {
        "id": "all",
        "name": "All layers",
        "description": "Everything, including signals and risk scoring.",
        "defences": {
            "preset": "all",
            "layers": {
                "rate_limit": {"enabled": True, "per_ip_rps": 5, "per_user_rps": 2, "burst": 10},
                "pow": {"enabled": True, "difficulty_bits": 18},
                "captcha": {"enabled": True, "provider": "mock", "when_risk_at_least": 0.7},
                "signals": {"enabled": True, "device_id": True, "honeypot": True, "timing": True},
                "risk": {"enabled": True, "challenge_at": 0.5, "reject_at": 0.9},
            },
        },
    },
]


@router.get("/defence/presets")
async def get_defence_presets() -> list[dict[str, Any]]:
    """Return available defence presets for event creation & config."""
    from app import hooks
    if (provided := hooks.defence_presets()) is not None:
        return provided
    return DEFENCE_PRESETS


async def _write(conn: AsyncConnection, key: str | None, endpoint: str, payload: Any,
                 op: Callable[[], Awaitable[tuple[int, dict[str, Any]]]]) -> JSONResponse:
    async with transaction(conn):
        status, body, replayed = await run_idempotent(
            conn, key=key, principal=ADMIN_PRINCIPAL, endpoint=endpoint, payload=payload, op=op)
    return JSONResponse(status_code=status, content=body,
                        headers={"Idempotent-Replayed": "true"} if replayed else None)


@router.post("/events", status_code=201, response_model=EventAdmin)
async def create_event(body: EventCreate, conn: AsyncConnection = Depends(get_conn),
                       idempotency_key: str | None = IdemKey):
    async def op():
        ev, now = await phases.create_event(conn, body)
        return 201, EventAdmin.build(ev, now).model_dump(mode="json")
    return await _write(conn, idempotency_key, "POST /admin/events", body.model_dump(mode="json"), op)


@router.get("/events", response_model=list[EventAdmin])
async def admin_list_events(conn: AsyncConnection = Depends(get_conn)) -> list[EventAdmin]:
    events, now = await list_events(conn, include_draft=True)
    return [EventAdmin.build(e, now) for e in events]


@router.get("/events/{event_id}", response_model=EventAdmin)
async def admin_get_event(event_id: UUID, conn: AsyncConnection = Depends(get_conn)) -> EventAdmin:
    found = await load_event(conn, event_id)
    if found is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Event not found")
    return EventAdmin.build(*found)


@router.get("/events/{event_id}/stats")
async def admin_get_stats(event_id: UUID, conn: AsyncConnection = Depends(get_conn)) -> dict:
    """Entry counts by state, seat usage and hold counts, read from the tables."""
    return await admin_stats.stats(conn, event_id)


@router.get("/events/{event_id}/invariants")
async def admin_get_invariants(event_id: UUID, conn: AsyncConnection = Depends(get_conn)) -> dict:
    """Re-derives the integrity properties (no oversell, one seat per holder and per user,
    no orphaned holds, entry state matches allocation) from the tables. `passed` is
    false if any is violated; `checks` says which."""
    return await admin_stats.invariants(conn, event_id)


def _transition(name: str, fn):
    async def handler(event_id: UUID, conn: AsyncConnection = Depends(get_conn),
                      idempotency_key: str | None = IdemKey):
        async def op():
            ev, now, changed = await fn(conn, event_id)
            return 200, TransitionResult(event=EventAdmin.build(ev, now), changed=changed).model_dump(mode="json")
        return await _write(conn, idempotency_key, f"POST /admin/events/{event_id}/{name}", {}, op)
    handler.__name__ = f"{name}_event"
    return handler


router.add_api_route(
    "/events/{event_id}/schedule", _transition("schedule", phases.schedule_event), methods=["POST"],
    response_model=TransitionResult,
    summary="DRAFT -> SCHEDULED: create seats, commit to server seed, fix beacon round",
)
router.add_api_route(
    "/events/{event_id}/open", _transition("open", phases.open_event), methods=["POST"],
    response_model=TransitionResult, summary="SCHEDULED -> OPEN now (manual override)",
)
router.add_api_route(
    "/events/{event_id}/close", _transition("close", phases.close_event_window), methods=["POST"],
    response_model=TransitionResult,
    summary="Close the entry window now: LOTTERY -> DRAWING, FCFS -> CLOSED",
)


@router.post("/events/{event_id}/draw", response_model=TransitionResult,
             summary="Run the lottery draw (closes the window first if its time has passed)")
async def draw_event(event_id: UUID, conn: AsyncConnection = Depends(get_conn)) -> TransitionResult:
    ev, now, changed = await draw_service.run_draw(conn, event_id)
    return TransitionResult(event=EventAdmin.build(ev, now), changed=changed)


@router.patch("/events/{event_id}/config", response_model=EventAdmin)
async def patch_event(event_id: UUID, body: EventPatch, conn: AsyncConnection = Depends(get_conn),
                      idempotency_key: str | None = IdemKey):
    async def op():
        ev, now = await phases.patch_event(conn, event_id, body)
        return 200, EventAdmin.build(ev, now).model_dump(mode="json")
    return await _write(conn, idempotency_key, f"PATCH /admin/events/{event_id}/config",
                        body.model_dump(mode="json", exclude_unset=True), op)
