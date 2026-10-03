"""Admin-driven event lifecycle: create, schedule, open, close, patch.

Concurrency: every transition locks the event row (SELECT ... FOR UPDATE)
and checks the current phase before writing, so two replicas or a replica and
the worker racing on the same transition serialise; the loser sees the new
phase and returns an idempotent no-op (changed=False) instead of applying the
transition twice.

Why FOR UPDATE (not FOR NO KEY UPDATE / plain UPDATE): the entry insert takes
FOR KEY SHARE on the event row (see services/entry.py). Only FOR UPDATE
conflicts with KEY SHARE, so closing the window waits for in-flight entry
transactions to commit and blocks new ones until the phase change is visible.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.errors import ApiError, ErrorCode
from app.schemas import EventCreate, EventPatch
from app.services import audit
from app.services.beacon import get_beacon
from app.services.events import EventInfo, load_event


async def _locked(conn: AsyncConnection, event_id: UUID) -> tuple[EventInfo, datetime]:
    found = await load_event(conn, event_id, for_update=True)
    if found is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Event not found")
    return found


async def _reload(conn: AsyncConnection, event_id: UUID) -> tuple[EventInfo, datetime]:
    found = await load_event(conn, event_id)
    assert found is not None
    return found


async def create_event(conn: AsyncConnection, body: EventCreate) -> tuple[EventInfo, datetime]:
    event_id = (await conn.execute(text("""
        INSERT INTO events (name, inventory, mode, window_opens_at, window_closes_at,
                            claim_ttl_seconds, claim_phase_seconds, config)
        VALUES (:name, :inventory, :mode, :opens, :closes, :ttl, :cps, CAST(:config AS jsonb))
        RETURNING id
    """), {
        "name": body.name, "inventory": body.inventory, "mode": body.mode,
        "opens": body.window_opens_at, "closes": body.window_closes_at,
        "ttl": body.claim_ttl_seconds, "cps": body.claim_phase_seconds,
        "config": json.dumps(body.config),
    })).scalar_one()
    await audit.record(conn, event_id, "event_created", body.model_dump(mode="json"))
    return await _reload(conn, event_id)


async def schedule_event(conn: AsyncConnection, event_id: UUID) -> tuple[EventInfo, datetime, bool]:
    """DRAFT -> SCHEDULED: create seats; for LOTTERY also generate and commit to
    the server seed and fix the beacon round (first round after the window closes)."""
    ev, now = await _locked(conn, event_id)
    if ev.phase != "DRAFT":
        return ev, now, False
    if ev.window_closes_at <= now:
        raise ApiError(ErrorCode.VALIDATION_ERROR,
                       "Cannot schedule: the entry window has already ended; patch the times first")

    commitment = beacon_round = None
    if ev.mode == "LOTTERY":
        seed = secrets.token_bytes(32)
        commitment = hashlib.sha256(seed).hexdigest()
        beacon_round = get_beacon().round_after(ev.window_closes_at)
        await conn.execute(
            text("INSERT INTO event_secrets (event_id, server_seed) VALUES (:id, :seed)"),
            {"id": event_id, "seed": seed.hex()},
        )

    await conn.execute(
        text("INSERT INTO seats (event_id, seat_no) SELECT :id, g FROM generate_series(1, :n) g"),
        {"id": event_id, "n": ev.inventory},
    )
    await conn.execute(text("""
        UPDATE events
           SET phase = 'SCHEDULED', seed_commitment = :c, beacon_round = :r,
               claim_phase_ends_at = window_closes_at + make_interval(secs => claim_phase_seconds),
               updated_at = now()
         WHERE id = :id AND phase = 'DRAFT'
    """), {"id": event_id, "c": commitment, "r": beacon_round})

    await audit.record(conn, event_id, "seats_created", {"count": ev.inventory})
    if commitment:
        await audit.record(conn, event_id, "commitment_published",
                           {"seed_commitment": commitment, "beacon_round": beacon_round,
                            "beacon": get_beacon().name})
    ev, now = await _reload(conn, event_id)
    return ev, now, True


async def open_event(conn: AsyncConnection, event_id: UUID) -> tuple[EventInfo, datetime, bool]:
    """SCHEDULED -> OPEN. Manual override: if the window has not started yet it
    starts now (window_opens_at := now), keeping I10 (entered_at within window) true."""
    ev, now = await _locked(conn, event_id)
    if ev.phase in ("OPEN", "DRAWING", "CLAIMING", "CLOSED"):
        return ev, now, False
    if ev.phase != "SCHEDULED":
        raise ApiError(ErrorCode.INVALID_PHASE, f"Cannot open an event in phase {ev.phase}; schedule it first")
    if ev.window_closes_at <= now:
        raise ApiError(ErrorCode.INVALID_PHASE, "Cannot open: the entry window has already ended")
    await conn.execute(text("""
        UPDATE events SET phase = 'OPEN', window_opens_at = LEAST(window_opens_at, fd_now()),
                          updated_at = now()
         WHERE id = :id AND phase = 'SCHEDULED'
    """), {"id": event_id})
    await audit.record(conn, event_id, "window_opened", {"manual": True})
    ev, now = await _reload(conn, event_id)
    return ev, now, True


async def close_event_window(conn: AsyncConnection, event_id: UUID) -> tuple[EventInfo, datetime, bool]:
    """Close the entry window now (manual override).

    LOTTERY: SCHEDULED|OPEN -> DRAWING (the draw then runs, stage 3).
    FCFS:    SCHEDULED|OPEN -> CLOSED.
    window_closes_at := min(closes_at, now). The beacon round committed at
    scheduling is NOT changed, so an early close may have to wait for that round.
    """
    ev, now = await _locked(conn, event_id)
    if ev.phase in ("DRAWING", "CLAIMING", "CLOSED"):
        return ev, now, False
    if ev.phase not in ("SCHEDULED", "OPEN"):
        raise ApiError(ErrorCode.INVALID_PHASE, f"Cannot close an event in phase {ev.phase}")
    target = "DRAWING" if ev.mode == "LOTTERY" else "CLOSED"
    # closes_at uses fd_clock_now() (wall clock, read now that we hold the row
    # lock), NOT fd_now() (our transaction start): an entry transaction that
    # started after us but locked the event first has already committed with
    # entered_at > our start time, and must still fall inside the window (I10).
    # opens_at is pulled back too if needed so the window stays non-empty
    # (CHECK closes > opens).
    await conn.execute(text("""
        UPDATE events
           SET phase = :target,
               window_opens_at  = LEAST(window_opens_at, fd_clock_now() - interval '1 microsecond'),
               window_closes_at = LEAST(window_closes_at, fd_clock_now()),
               closed_at = CASE WHEN :target = 'CLOSED' THEN fd_now() ELSE closed_at END,
               updated_at = now()
         WHERE id = :id AND phase IN ('SCHEDULED', 'OPEN')
    """), {"id": event_id, "target": target})
    await audit.record(conn, event_id, "window_closed", {"manual": True})
    ev, now = await _reload(conn, event_id)
    return ev, now, True


async def patch_event(conn: AsyncConnection, event_id: UUID, body: EventPatch) -> tuple[EventInfo, datetime]:
    ev, now = await _locked(conn, event_id)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return ev, now
    draft_only = {"mode", "window_opens_at", "window_closes_at", "claim_ttl_seconds", "claim_phase_seconds"}
    if draft_only & fields.keys() and ev.phase != "DRAFT":
        raise ApiError(ErrorCode.INVALID_PHASE,
                       f"{sorted(draft_only & fields.keys())} can only change while the event is DRAFT")

    opens = fields.get("window_opens_at", ev.window_opens_at)
    closes = fields.get("window_closes_at", ev.window_closes_at)
    if closes <= opens:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "window_closes_at must be after window_opens_at")

    params: dict[str, Any] = {"id": event_id}
    sets = []
    for col in ("mode", "window_opens_at", "window_closes_at", "claim_ttl_seconds", "claim_phase_seconds"):
        if col in fields:
            sets.append(f"{col} = :{col}")
            params[col] = fields[col]
    if "config" in fields:
        sets.append("config = CAST(:config AS jsonb)")
        params["config"] = json.dumps(fields["config"])
    await conn.execute(text(f"UPDATE events SET {', '.join(sets)}, updated_at = now() WHERE id = :id"), params)
    await audit.record(conn, event_id, "event_patched", body.model_dump(mode="json", exclude_unset=True))
    return await _reload(conn, event_id)
