"""Reading events. Public code paths only ever read the `events` table, never
`event_secrets` (the server seed lives there until the reveal)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

EVENT_COLUMNS = """
    ev.id, ev.name, ev.inventory, ev.mode, ev.phase, ev.window_opens_at, ev.window_closes_at,
    ev.claim_ttl_seconds, ev.claim_phase_seconds, ev.claim_phase_ends_at, ev.seed_commitment,
    ev.beacon_round, ev.beacon_randomness, ev.beacon_signature, ev.entrants_hash,
    ev.entrant_count, ev.final_seed, ev.revealed_seed, ev.algorithm_version, ev.drawn_at,
    ev.closed_at, ev.config, ev.created_at, ev.updated_at
"""


@dataclass(frozen=True)
class EventInfo:
    id: UUID
    name: str
    inventory: int
    mode: str
    phase: str
    window_opens_at: datetime
    window_closes_at: datetime
    claim_ttl_seconds: int
    claim_phase_seconds: int
    claim_phase_ends_at: datetime | None
    seed_commitment: str | None
    beacon_round: int | None
    beacon_randomness: str | None
    beacon_signature: str | None
    entrants_hash: str | None
    entrant_count: int | None
    final_seed: str | None
    revealed_seed: str | None
    algorithm_version: str
    drawn_at: datetime | None
    closed_at: datetime | None
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> EventInfo:
        return cls(**{f: getattr(row, f) for f in cls.__dataclass_fields__})

    def window_status(self, now: datetime) -> str:
        return window_status(self.phase, self.window_opens_at, self.window_closes_at, now)


def window_status(phase: str, opens_at: datetime, closes_at: datetime, now: datetime) -> str:
    """Entry window state: BEFORE | OPEN | CLOSED.

    Reported next to `phase` so clients see the truth even if the worker has
    not yet flipped the phase. The window is [opens_at, closes_at); a phase past
    OPEN always means CLOSED, whatever the timestamps say.
    """
    if phase in ("DRAWING", "CLAIMING", "CLOSED") or now >= closes_at:
        return "CLOSED"
    if now < opens_at:
        return "BEFORE"
    return "OPEN"


async def load_event(conn: AsyncConnection, event_id: UUID, *, for_update: bool = False
                     ) -> tuple[EventInfo, datetime] | None:
    """Event row plus server time (one round trip)."""
    lock = " FOR UPDATE OF ev" if for_update else ""
    row = (await conn.execute(
        text(f"SELECT {EVENT_COLUMNS}, fd_now() AS server_now FROM events ev WHERE ev.id = :id{lock}"),
        {"id": event_id},
    )).first()
    if row is None:
        return None
    return EventInfo.from_row(row), row.server_now


async def list_events(conn: AsyncConnection, *, include_draft: bool
                      ) -> tuple[list[EventInfo], datetime]:
    where = "" if include_draft else "WHERE ev.phase <> 'DRAFT'"
    rows = (await conn.execute(text(
        f"SELECT {EVENT_COLUMNS}, fd_now() AS server_now FROM events ev {where} "
        "ORDER BY ev.window_opens_at, ev.id"
    ))).all()
    now = rows[0].server_now if rows else (await conn.execute(text("SELECT fd_now()"))).scalar_one()
    return [EventInfo.from_row(r) for r in rows], now
