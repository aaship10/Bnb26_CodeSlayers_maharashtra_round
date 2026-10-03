"""Idempotency-Key support (claim and admin writes).

Protocol, all inside the caller's transaction:
  1. pg_advisory_xact_lock(key, principal, endpoint): concurrent requests with
     the same key queue up behind each other instead of both executing.
  2. If a stored response exists: same request body -> replay it verbatim;
     different body -> 422 IDEMPOTENCY_KEY_REUSED.
  3. Otherwise run the operation and store its response in the SAME
     transaction, so "key stored" <=> "operation committed". A crash or error
     rolls both back and the client can safely retry.

Only successful responses are stored; a failed attempt is simply re-evaluated
on retry (all our operations are themselves state-based and safe to re-run).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.errors import ApiError, ErrorCode

ADMIN_PRINCIPAL = UUID(int=0)
MAX_KEY_LEN = 200

Operation = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]


def request_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def run_idempotent(conn: AsyncConnection, *, key: str | None, principal: UUID,
                         endpoint: str, payload: Any, op: Operation) -> tuple[int, dict[str, Any], bool]:
    """Returns (status_code, body, replayed). Caller must hold an open transaction."""
    if key is None:
        status, body = await op()
        return status, body, False
    if not 1 <= len(key) <= MAX_KEY_LEN:
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"Idempotency-Key must be 1..{MAX_KEY_LEN} chars")

    rhash = request_hash(payload)
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"idem|{principal}|{endpoint}|{key}"},
    )
    row = (await conn.execute(
        text("SELECT request_hash, status_code, response_body FROM idempotency_keys "
             "WHERE key = :k AND user_id = :u AND endpoint = :e"),
        {"k": key, "u": principal, "e": endpoint},
    )).first()
    if row is not None:
        if row.request_hash != rhash:
            raise ApiError(ErrorCode.IDEMPOTENCY_KEY_REUSED,
                           "Idempotency-Key was already used with a different request")
        return row.status_code, row.response_body, True

    status, body = await op()
    await conn.execute(
        text("INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash, status_code, response_body) "
             "VALUES (:k, :u, :e, :h, :s, CAST(:b AS jsonb))"),
        {"k": key, "u": principal, "e": endpoint, "h": rhash, "s": status, "b": json.dumps(body)},
    )
    return status, body, False
