"""The single source of time.

All deadlines are evaluated with the SQL function fd_now() (defined in the
initial migration): database transaction time plus an offset stored in the
one-row dev_clock table. Because the offset lives in the database, shifting the
clock in a test or demo is seen consistently by every API replica and worker.
Client clocks are never consulted.

fd_now() is based on now(), i.e. the start time of the current transaction, so
every comparison inside one transaction sees the same instant.
"""
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import get_settings


async def server_now(conn: AsyncConnection) -> datetime:
    return (await conn.execute(text("SELECT fd_now()"))).scalar_one()


async def set_clock_offset(conn: AsyncConnection, seconds: float) -> None:
    """Dev/test only: move server time for everyone. Caller commits."""
    if not get_settings().is_dev:
        raise RuntimeError("clock offset can only be changed in APP_ENV=dev")
    await conn.execute(
        text("UPDATE dev_clock SET offset_seconds = :s WHERE id"), {"s": seconds}
    )


async def advance_clock(conn: AsyncConnection, seconds: float) -> None:
    """Dev/test only: move server time forward by `seconds`. Caller commits."""
    if not get_settings().is_dev:
        raise RuntimeError("clock offset can only be changed in APP_ENV=dev")
    await conn.execute(
        text("UPDATE dev_clock SET offset_seconds = offset_seconds + :s WHERE id"),
        {"s": seconds},
    )
