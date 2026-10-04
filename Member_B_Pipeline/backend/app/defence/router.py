"""Routes owned by Member B: presets, CAPTCHA waivers (admin), challenge pre-fetch (attendee)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import lifecycle, metrics
from .admin_auth import require_admin
from .captcha import waivers
from .errors import ApiError, ErrorCode
from .identity.deps import get_current_user
from .presets import list_presets
from .ratelimit.gate import enforce
from .runtime import get_runtime
from .timeutil import iso_z, utcnow

admin_router = APIRouter(prefix="/admin/defence", tags=["defence-admin"], dependencies=[Depends(require_admin)])
public_router = APIRouter(prefix="/defence", tags=["defence"])


@admin_router.get("/presets")
async def get_presets() -> list[dict]:
    # A bare array of {id, name, description, defences}: the shape the organizer UI (D) consumes.
    return list_presets()


# ------------------------------------------------------------------ accessible fallback
class WaiverBody(BaseModel):
    event_id: uuid.UUID
    user_id: uuid.UUID
    reason: str = Field(default="", max_length=300)


@admin_router.post("/waivers", status_code=201)
async def grant_waiver(body: WaiverBody) -> dict:
    """Organiser waives the CAPTCHA (not the proof-of-work) for one person on one event."""
    await waivers.grant(await get_runtime(), body.event_id, body.user_id, body.reason)
    return {"event_id": str(body.event_id), "user_id": str(body.user_id), "waived": ["captcha"]}


@admin_router.delete("/waivers")
async def revoke_waiver(event_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    if not await waivers.revoke(await get_runtime(), event_id, user_id):
        raise ApiError(ErrorCode.NOT_FOUND, "no such waiver")
    return {"revoked": True}


@admin_router.get("/waivers")
async def list_waivers(event_id: uuid.UUID) -> list[dict]:
    return await waivers.listing(await get_runtime(), event_id)


# ----------------------------------------------------------------------- pre-fetch
class ChallengeBody(BaseModel):
    event_id: uuid.UUID


@public_router.post("/challenge")
async def prefetch_challenge(body: ChallengeBody, request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Optional: fetch the challenge ahead of time so the browser can solve it BEFORE pressing Enter.
    Returns the same object CHALLENGE_REQUIRED would carry. 404 (reason no_challenge_required) if the
    event needs none from this person."""
    from .gate import required_challenge  # local import: gate imports the runtime, which imports routes' siblings

    await enforce(request, "challenge", body.event_id)
    ch = await required_challenge(body.event_id, user["id"], utcnow())
    if ch is None:
        raise ApiError(ErrorCode.NOT_FOUND, "no challenge required", details={"reason": "no_challenge_required"})
    return {**ch.to_dict(), "server_now": iso_z(utcnow())}


# ------------------------------------------------------------------------ decision log (for C)
def _decision_json(r: dict) -> dict:
    return {
        "id": r["id"], "event_id": str(r["event_id"]), "user_id": str(r["user_id"]), "ts": iso_z(r["ts"]),
        "action": r["action"], "weight": r["weight"], "score": r["score"], "signals": r["signals"],
        "layer": r["layer"], "ip": r["ip"], "device": r["device"], "reason": r["reason"],
    }


@admin_router.get("/decisions")
async def get_decisions(event_id: uuid.UUID, cursor: int = 0, limit: int = 1000, action: str | None = None) -> dict:
    """Keyset-paginated gate decisions for one event: pass `next_cursor` back as `cursor`. Order is by row id
    (INSERT order). With several replicas, batches interleave, so for chronology sort by `ts`.
    For Member C's detection precision/recall: join `user_id` with your own ground truth. This table has
    no ground-truth column and the defence code never reads one."""
    if not 1 <= limit <= 5000:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "limit must be within 1..5000")
    if action is not None and action not in ("ALLOW", "REJECT", "CHALLENGE"):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "action must be ALLOW, REJECT or CHALLENGE")
    rt = await get_runtime()
    if rt.decisions is not None:
        await rt.decisions.flush()  # so a reader sees everything recorded so far on THIS replica
    sql = """SELECT id, event_id, user_id, ts, action, weight, score, signals, layer, host(ip) AS ip, device, reason
               FROM defence.decisions WHERE event_id = %s AND id > %s"""
    params: list = [event_id, cursor]
    if action:
        sql += " AND action = %s"
        params.append(action)
    sql += " ORDER BY id LIMIT %s"
    params.append(limit)
    async with rt.pg.connection() as conn:
        rows = await (await conn.execute(sql, tuple(params))).fetchall()
    return {"decisions": [_decision_json(r) for r in rows], "next_cursor": rows[-1]["id"] if len(rows) == limit else None,
            "server_now": iso_z(utcnow())}


@admin_router.get("/decisions/summary")
async def decisions_summary(event_id: uuid.UUID) -> dict:
    rt = await get_runtime()
    if rt.decisions is not None:
        await rt.decisions.flush()
    async with rt.pg.connection() as conn:
        rows = await (await conn.execute(
            """SELECT action, weight, layer, count(*) AS n FROM defence.decisions WHERE event_id = %s
                GROUP BY action, weight, layer ORDER BY action, weight, layer""", (event_id,))).fetchall()
    return {"event_id": str(event_id), "counts": [{"action": r["action"], "weight": r["weight"], "layer": r["layer"], "n": r["n"]} for r in rows],
            "log": {"written": rt.decisions.written if rt.decisions else 0, "dropped": rt.decisions.dropped if rt.decisions else 0,
                    "failed_batches": rt.decisions.failed_batches if rt.decisions else 0}, "server_now": iso_z(utcnow())}


# ------------------------------------------------------------------------- metrics + lifecycle
class DrainBody(BaseModel):
    exit_after_s: float | None = Field(default=None, ge=0, le=600)


@admin_router.post("/lifecycle/drain")
async def drain(body: DrainBody | None = None) -> dict:
    """Start draining THIS replica (rolling restarts, or a demo): readiness flips to 503 immediately and open
    streams are told to reconnect. With exit_after_s the process then stops after that many seconds, once
    in-flight requests have finished. The SIGTERM handler runs the same code."""
    first = lifecycle.begin_drain(exit_after_s=body.exit_after_s if body else None, reason="admin request")
    return {"draining": True, "started_now": first}


@admin_router.get("/lifecycle")
async def lifecycle_status() -> dict:
    return {"draining": lifecycle.is_draining()}


metrics_router = APIRouter(include_in_schema=False)


@metrics_router.get("/metrics")
async def prometheus_metrics() -> Response:
    """Per-replica Prometheus exposition. Scraped directly from each replica; the public gateway/nginx must NOT
    proxy it (it hides /api/metrics)."""
    body, ctype = metrics.render()
    return Response(body, media_type=ctype)
