"""Async database engine.

The API is stateless: every request borrows a pooled connection and all state
is read from / written to Postgres, so any number of replicas can run.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            s.database_url,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_pre_ping=True,
        )
    return _engine


def set_engine(engine: AsyncEngine | None) -> None:
    """Test hook: point the app at a different database."""
    global _engine
    _engine = engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def transaction(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """Explicit transaction on a request connection.

    Dependencies (e.g. auth) may have run a read that implicitly began a
    transaction; end it first so the critical section below starts with a
    fresh transaction (fresh fd_now(), no stale locks or snapshots).
    """
    if conn.in_transaction():
        await conn.commit()
    async with conn.begin():
        yield conn


async def get_conn() -> AsyncIterator[AsyncConnection]:
    """FastAPI dependency: one connection per request, no implicit transaction.

    Handlers open explicit transactions with `async with conn.begin()` so the
    transaction boundary is visible next to the locking logic.
    """
    async with get_engine().connect() as conn:
        yield conn
