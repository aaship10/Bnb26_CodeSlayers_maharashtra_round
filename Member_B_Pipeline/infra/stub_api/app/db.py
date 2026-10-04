"""Postgres access for the stub (psycopg 3 async pool) and its schema bootstrap."""
from __future__ import annotations

import os
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

SEED_EVENT_ID = "11111111-1111-1111-1111-111111111111"

_pool: AsyncConnectionPool | None = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email        text NOT NULL UNIQUE,
    display_name text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS events (
    id           uuid PRIMARY KEY,
    name         text NOT NULL,
    inventory    integer NOT NULL CHECK (inventory > 0),
    window_start timestamptz NOT NULL,
    window_end   timestamptz NOT NULL,
    config       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS entries (
    id         bigserial PRIMARY KEY,
    event_id   uuid NOT NULL REFERENCES events(id),
    user_id    uuid NOT NULL REFERENCES users(id),
    weight     numeric(3,2) NOT NULL CHECK (weight IN (1.00, 0.50, 0.25)),
    risk       jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, user_id)
);
"""


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool is not open")
    return _pool


async def open_pool() -> None:
    global _pool
    dsn = os.environ["DATABASE_URL"]
    size = int(os.environ.get("DB_POOL_SIZE", "10"))
    _pool = AsyncConnectionPool(dsn, min_size=1, max_size=size, kwargs={"row_factory": dict_row}, check=AsyncConnectionPool.check_connection, open=False)
    await _pool.open(wait=True, timeout=30)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_schema() -> None:
    hours = int(os.environ.get("STUB_WINDOW_HOURS", "168"))
    async with get_pool().connection() as conn:
        # Several replicas boot at once; the xact lock serialises schema creation
        # (CREATE TABLE IF NOT EXISTS is not race-free on its own).
        await conn.execute("SELECT pg_advisory_xact_lock(727001)")
        await conn.execute(SCHEMA_SQL)
        await conn.execute(
            """
            INSERT INTO events (id, name, inventory, window_start, window_end)
            VALUES (%s, 'Fair Drop demo event', 500, now(), now() + make_interval(hours => %s))
            ON CONFLICT (id) DO NOTHING
            """,
            (SEED_EVENT_ID, hours),
        )


async def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    async with get_pool().connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with get_pool().connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()
