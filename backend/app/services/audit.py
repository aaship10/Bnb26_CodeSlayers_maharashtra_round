"""Audit log appends.

Stage 2 placeholder: callers already record every auditable action here, in the
same transaction as the action. Stage 6 replaces the body with the hash-chained
append (per-event advisory lock + SHA-256 chain); call sites will not change.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection


async def record(conn: AsyncConnection, event_id: UUID | None, type_: str,
                 payload: dict[str, Any]) -> None:
    return None
