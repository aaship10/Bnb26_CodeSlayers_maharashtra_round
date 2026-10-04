"""Accessible fallback: an organiser waives the CAPTCHA for one person on one event.

The frontend tells people who cannot use the CAPTCHA to "tell the organisers"; this is what the
organiser then does (POST /admin/defence/waivers). A waiver skips ONLY the CAPTCHA. Proof-of-work still
applies: it is non-visual and solved by the browser automatically, so it is not an accessibility barrier.
Durable (Postgres), not cached state: a waiver must survive restarts and be visible on every replica.
"""
from __future__ import annotations

import uuid
from typing import Any

from ..runtime import Runtime


async def is_waived(rt: Runtime, event_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    async with rt.pg.connection() as conn:
        row = await (
            await conn.execute("SELECT 1 FROM defence.waivers WHERE event_id = %s AND user_id = %s", (event_id, user_id))
        ).fetchone()
    return row is not None


async def grant(rt: Runtime, event_id: uuid.UUID, user_id: uuid.UUID, reason: str) -> None:
    async with rt.pg.connection() as conn:
        await conn.execute(
            """INSERT INTO defence.waivers (event_id, user_id, reason) VALUES (%s, %s, %s)
               ON CONFLICT (event_id, user_id) DO UPDATE SET reason = EXCLUDED.reason""",
            (event_id, user_id, reason),
        )


async def revoke(rt: Runtime, event_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    async with rt.pg.connection() as conn:
        cur = await conn.execute("DELETE FROM defence.waivers WHERE event_id = %s AND user_id = %s", (event_id, user_id))
        return cur.rowcount > 0


async def listing(rt: Runtime, event_id: uuid.UUID) -> list[dict[str, Any]]:
    async with rt.pg.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT user_id, reason, created_at FROM defence.waivers WHERE event_id = %s ORDER BY created_at", (event_id,)
            )
        ).fetchall()
    return [{"user_id": str(r["user_id"]), "reason": r["reason"], "created_at": r["created_at"].isoformat()} for r in rows]
