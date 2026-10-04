"""STUB of Member A's API: only what the defence layers need to be exercised.

NOT allocation logic: no draw, no claim, no waitlist. Entry is "unique per user
per event, gated by entry_gate", which is the contract A's real /enter honours.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import psycopg.errors
from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import db, hooks
from app.auth import get_current_user
from app.defence import install, lifecycle, metrics
from app.defence.admin_auth import require_admin
from app.defence.config import DefenceConfigError, validate_defences
from app.defence.errors import ApiError, ErrorCode
from app.defence.ratelimit.gate import rate_limit
from app.defence.settings import get_settings
from app.defence.timeutil import iso_z, utcnow
from app.defence.contracts import ALLOWED_WEIGHTS, Action, GateContext


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.open_pool()
    await db.init_schema()
    yield
    await db.close_pool()


app = FastAPI(title="Fair Drop STUB backend", lifespan=lifespan)
install(app)  # error handlers + defence routers (one line, same as A will do)


# ------------------------------------------------------------------ health
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    if lifecycle.is_draining():
        # 503 tells the gateway/nginx to stop sending NEW requests here while in-flight ones finish.
        raise ApiError(ErrorCode.INTERNAL, "draining", status=503, headers={"Retry-After": "1"})
    try:
        await db.fetch_one("SELECT 1 AS ok")
    except Exception:
        raise ApiError(ErrorCode.INTERNAL, "database unavailable", status=503) from None
    return {"status": "ready"}


# --------------------------------------------------------------- dev users
class DevUser(BaseModel):
    email: str
    display_name: str


@app.get("/dev/slow")
async def dev_slow(seconds: float = 1.0) -> dict[str, float]:
    """Dev-only: a request that takes `seconds`, to test that in-flight requests finish during a drain."""
    if get_settings().env != "dev":
        raise ApiError(ErrorCode.NOT_FOUND, "not found")
    await asyncio.sleep(min(max(seconds, 0.0), 30.0))
    return {"slept": seconds}


@app.post("/dev/users", status_code=201)
async def dev_create_user(body: DevUser) -> dict[str, str]:
    """Dev-only convenience so AUTH_MODE=dev works before stage 2's real registration."""
    if get_settings().auth_mode != "dev":
        raise ApiError(ErrorCode.NOT_FOUND, "not found")
    row = await db.fetch_one(
        """INSERT INTO users (email, display_name) VALUES (lower(%s), %s)
           ON CONFLICT (email) DO UPDATE SET display_name = EXCLUDED.display_name
           RETURNING id""",
        (body.email, body.display_name),
    )
    assert row is not None
    return {"user_id": str(row["id"])}


# ------------------------------------------------------------------ events
async def _event_or_404(event_id: uuid.UUID) -> dict[str, Any]:
    ev = await db.fetch_one("SELECT * FROM events WHERE id = %s", (event_id,))
    if ev is None:
        raise ApiError(ErrorCode.NOT_FOUND, "event not found")
    return ev


def _window_state(ev: dict[str, Any], now: datetime) -> str:
    if now < ev["window_start"]:
        return "not_open"
    if now >= ev["window_end"]:
        return "closed"
    return "open"


def _phase(ev: dict[str, Any], now: datetime) -> str:
    """The stub has no draw/claim: before the window SCHEDULED, during it OPEN, after it CLOSED."""
    return {"not_open": "SCHEDULED", "open": "OPEN", "closed": "CLOSED"}[_window_state(ev, now)]


def _event_body(ev: dict[str, Any], now: datetime) -> dict[str, Any]:
    # Field names are the frontend's schema (src/api/schemas.ts eventSchema).
    return {
        "id": str(ev["id"]),
        "name": ev["name"],
        "description": "Stub event for exercising the defence layers. The draw and claim are Member A's.",
        "phase": _phase(ev, now),
        "mode": "LOTTERY",
        "inventory": ev["inventory"],
        "window_opens_at": iso_z(ev["window_start"]),
        "window_closes_at": iso_z(ev["window_end"]),
        "claim_ttl_s": 600,
        "seed_commitment": None,
        "server_now": iso_z(now),
    }


@app.get("/events")
async def list_events() -> list[dict[str, Any]]:
    now = utcnow()
    rows = await db.fetch_all("SELECT * FROM events ORDER BY window_start")
    return [_event_body(r, now) for r in rows]


@app.get("/events/{event_id}")
async def get_event(event_id: uuid.UUID) -> dict[str, Any]:
    return _event_body(await _event_or_404(event_id), utcnow())


async def _existing_entry(event_id: uuid.UUID, user_id: Any) -> dict[str, Any] | None:
    return await db.fetch_one(
        "SELECT weight, risk, created_at FROM entries WHERE event_id = %s AND user_id = %s", (event_id, user_id)
    )


def _entry_body(row: dict[str, Any], now: datetime, already: bool) -> dict[str, Any]:
    # The weight is deliberately NOT returned: it is an internal allocation input, and the
    # frontend contract (enterResponseSchema) has no field for it.
    return {
        "state": "ENTERED",
        "already_entered": already,
        "entered_at": iso_z(row["created_at"]),
        "server_now": iso_z(now),
    }


@app.post("/events/{event_id}/enter", dependencies=[Depends(rate_limit("enter"))])
async def enter(event_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)):
    ev = await _event_or_404(event_id)
    now = utcnow()

    # Contract with the gate: the already-entered fast path runs BEFORE entry_gate,
    # so a repeated /enter is idempotent and never costs a challenge.
    existing = await _existing_entry(event_id, user["id"])
    if existing is not None:
        return _entry_body(existing, now, already=True)

    state = _window_state(ev, now)
    if state == "not_open":
        raise ApiError(ErrorCode.WINDOW_NOT_OPEN, "entry window has not opened", details={"server_now": iso_z(now)})
    if state == "closed":
        raise ApiError(ErrorCode.WINDOW_CLOSED, "entry window is closed", details={"server_now": iso_z(now)})

    decision = await hooks.entry_gate(
        GateContext(user=user, event=ev, request=request, server_now=now, already_entered=False)
    )
    if decision.action is Action.CHALLENGE:
        assert decision.challenge is not None
        raise ApiError(
            ErrorCode.CHALLENGE_REQUIRED,
            decision.reason or "challenge required",
            details={"challenge": decision.challenge.to_dict()},
        )
    if decision.action is Action.REJECT:
        raise ApiError(ErrorCode.REJECTED, decision.reason or "rejected", details={"reason": decision.reason})
    assert decision.weight in ALLOWED_WEIGHTS

    try:
        row = await db.fetch_one(
            """INSERT INTO entries (event_id, user_id, weight, risk) VALUES (%s, %s, %s, %s::jsonb)
               ON CONFLICT (event_id, user_id) DO NOTHING
               RETURNING weight, risk, created_at""",
            (event_id, user["id"], decision.weight, json.dumps(decision.risk) if decision.risk is not None else None),
        )
    except psycopg.errors.ForeignKeyViolation:
        # get_current_user does no DB lookup (stateless JWT), so a validly signed token or
        # simulation id can name a user the database does not have.
        raise ApiError(ErrorCode.UNAUTHENTICATED, "unknown user") from None
    if row is None:  # lost a race with a concurrent request from the same user
        row = await _existing_entry(event_id, user["id"])
        assert row is not None
        return _entry_body(row, now, already=True)
    return JSONResponse(_entry_body(row, now, already=False), status_code=201)


async def _status_body(ev: dict[str, Any], user_id: Any) -> dict[str, Any]:
    now = utcnow()
    entry = await _existing_entry(ev["id"], user_id)
    return {"state": "ENTERED" if entry else "REGISTERED", "phase": _phase(ev, now), "server_now": iso_z(now)}


@app.get("/events/{event_id}/status", dependencies=[Depends(rate_limit("status"))])
async def status(event_id: uuid.UUID, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await _status_body(await _event_or_404(event_id), user["id"])


@app.get("/events/{event_id}/stream", dependencies=[Depends(rate_limit("status"))])
async def stream(event_id: uuid.UUID, user: dict = Depends(get_current_user), last_event_id: str | None = Header(default=None)):
    """Server-sent events in A's wire format (docs/INTERFACE_REQUESTS_D.md A2/A3): identity from headers,
    `id:` integers continuing from Last-Event-ID, `event: status` with the same JSON as /status, `retry: 3000`,
    a heartbeat comment at least every 20 s. The STUB sends the current status on every (re)connect (the real
    engine replays missed changes). When the replica drains, the stream ends with `event: reconnect` and a
    `retry:` hint so the browser reconnects through the gateway to a healthy replica."""
    if lifecycle.is_draining():
        # A NEW stream on a draining replica is refused with 503 so an edge that cannot see readiness (nginx) retries it on
        # another replica; browsers behind the dev gateway never get here (it stops routing to a draining replica).
        raise ApiError(ErrorCode.INTERNAL, "draining", status=503, details={"reason": "server_draining", "retry_after_ms": 1000},
                       headers={"X-Draining": "1", "Retry-After": "1"})
    ev = await _event_or_404(event_id)
    heartbeat_s = float(os.environ.get("SSE_HEARTBEAT_S", "15"))
    poll_s = float(os.environ.get("SSE_POLL_S", "1"))
    seq = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    async def events():
        nonlocal seq
        metrics.SSE_OPEN.inc()
        try:
            yield "retry: 3000\n\n"
            last_key, last_sent = None, time.monotonic()
            while True:
                if lifecycle.is_draining():
                    metrics.SSE_CLOSED_FOR_DRAIN.inc()
                    yield 'event: reconnect\ndata: {"reason":"server_draining"}\nretry: 1000\n\n'
                    return
                body = await _status_body(ev, user["id"])
                key = (body["state"], body["phase"])
                if key != last_key:
                    seq += 1
                    yield f"id: {seq}\nevent: status\ndata: {json.dumps(body)}\n\n"
                    last_key, last_sent = key, time.monotonic()
                elif time.monotonic() - last_sent >= heartbeat_s:
                    yield ": heartbeat\n\n"
                    last_sent = time.monotonic()
                try:  # wake early when a drain starts
                    await asyncio.wait_for(lifecycle.drain_event().wait(), poll_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            metrics.SSE_OPEN.dec()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------- admin (stub)
@app.patch("/admin/events/{event_id}/config", dependencies=[Depends(require_admin)])
async def patch_event_config(event_id: uuid.UUID, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """A's real endpoint treats config as opaque. The stub additionally validates
    `defences` so toggling presets can be tested now; see INTERFACE_REQUESTS_B R7."""
    await _event_or_404(event_id)
    normalised: dict[str, Any] = {}
    if "defences" in body:
        try:
            normalised["defences"] = validate_defences(body["defences"]).model_dump(mode="json")
        except DefenceConfigError as exc:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "invalid defences config", details={"errors": exc.errors}) from exc
    merged = {**{k: v for k, v in body.items() if k != "defences"}, **normalised}
    row = await db.fetch_one(
        "UPDATE events SET config = config || %s::jsonb WHERE id = %s RETURNING config",
        (json.dumps(merged), event_id),
    )
    assert row is not None
    return {"config": row["config"], "server_now": iso_z(utcnow())}


@app.get("/admin/events/{event_id}/invariants", dependencies=[Depends(require_admin)])
async def invariants(event_id: uuid.UUID) -> dict[str, Any]:
    """Stand-in for A's invariant checker so chaos scripts (stage 7) have a target."""
    await _event_or_404(event_id)
    dup = await db.fetch_one(
        """SELECT count(*) AS n FROM (
               SELECT user_id FROM entries WHERE event_id = %s GROUP BY user_id HAVING count(*) > 1) d""",
        (event_id,),
    )
    bad_w = await db.fetch_one(
        "SELECT count(*) AS n FROM entries WHERE event_id = %s AND weight NOT IN (1.00, 0.50, 0.25)", (event_id,)
    )
    checks = [
        {"name": "one_entry_per_user", "passed": dup is not None and dup["n"] == 0},
        {"name": "weights_in_allowed_set", "passed": bad_w is not None and bad_w["n"] == 0},
    ]
    now = iso_z(utcnow())
    # Shape = Member D's `invariantsSchema` (frontend/src/features/admin/schemas.ts); `checks` is extra detail for scripts.
    # The stub has no seats or holds, so those counters are honestly 0 (A's real checker computes them).
    return {
        "oversold": 0, "duplicate_users": dup["n"] if dup else 0, "duplicate_seats": 0, "orphaned_holds": 0,
        "passed": all(c["passed"] for c in checks), "checked_at": now, "server_now": now, "checks": checks,
    }
