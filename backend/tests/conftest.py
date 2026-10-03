"""Shared fixtures.

Integration tests run against a real Postgres (TEST_DATABASE_URL) in a
separate database, `fairdrop_test`. The schema is rebuilt from the Alembic
migrations once per session (downgrade to base, upgrade to head), which also
proves the migration is reversible. Every test starts from empty tables.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from psycopg import sql

from app import hooks
from app.config import get_settings, sync_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_URL = get_settings().test_database_url

TABLES = "idempotency_keys, allocations, entries, seats, event_secrets, events, users"


def alembic_config(url: str) -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


def _dbname(url: str) -> str:
    return psycopg.conninfo.conninfo_to_dict(sync_url(url))["dbname"]


def _target(url: str) -> tuple:
    info = psycopg.conninfo.conninfo_to_dict(sync_url(url))
    return info.get("host"), info.get("port"), info["dbname"]


def _ensure_database(test_url: str, main_url: str) -> None:
    """Create the test database if missing.

    Connects through the main DATABASE_URL (hosted providers such as Neon may
    not expose a `postgres` maintenance database) and issues CREATE DATABASE.
    """
    if _target(test_url) == _target(main_url):
        pytest.exit("TEST_DATABASE_URL must differ from DATABASE_URL: tests wipe their database", returncode=3)
    dbname = _dbname(test_url)
    with psycopg.connect(sync_url(main_url), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


@pytest.fixture(scope="session")
def migrated_db() -> str:
    try:
        _ensure_database(TEST_URL, get_settings().database_url)
    except psycopg.OperationalError as exc:  # pragma: no cover - environment problem
        pytest.exit(f"Test Postgres unreachable ({exc}). Check DATABASE_URL / TEST_DATABASE_URL in .env", returncode=3)
    cfg = alembic_config(TEST_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return TEST_URL


def wipe(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL fairdrop.allow_audit_reset = 'on'")
        conn.execute("DELETE FROM audit_log")
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")
        conn.execute("UPDATE dev_clock SET offset_seconds = 0 WHERE id")


@pytest.fixture
def db(migrated_db: str) -> Iterator[psycopg.Connection]:
    """Autocommit sync connection to a freshly wiped test database."""
    with psycopg.connect(sync_url(migrated_db), autocommit=True) as conn:
        wipe(conn)
        yield conn


# ------------------------------------------------------------------ factories
def make_user(conn: psycopg.Connection, sim_label: str | None = None) -> uuid.UUID:
    uid = uuid.uuid4()
    conn.execute(
        "INSERT INTO users (id, email, display_name, sim_label) VALUES (%s, %s, 'T', %s)",
        (uid, f"{uid}@test.local", sim_label),
    )
    return uid


def make_event(conn: psycopg.Connection, inventory: int = 5, mode: str = "LOTTERY",
               with_seats: bool = True) -> uuid.UUID:
    """A DRAFT event whose window is currently open (now-1m .. now+10m)."""
    eid = conn.execute(
        """
        INSERT INTO events (name, inventory, mode, window_opens_at, window_closes_at,
                            claim_ttl_seconds, claim_phase_seconds)
        VALUES ('test', %s, %s, fd_now() - interval '1 minute', fd_now() + interval '10 minutes', 60, 600)
        RETURNING id
        """,
        (inventory, mode),
    ).fetchone()[0]
    if with_seats:
        conn.execute(
            "INSERT INTO seats (event_id, seat_no) SELECT %s, g FROM generate_series(1, %s) g",
            (eid, inventory),
        )
    return eid


def make_entry(conn: psycopg.Connection, event_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return conn.execute(
        "INSERT INTO entries (event_id, user_id) VALUES (%s, %s) RETURNING id", (event_id, user_id)
    ).fetchone()[0]


def seat_id(conn: psycopg.Connection, event_id: uuid.UUID, seat_no: int) -> int:
    return conn.execute(
        "SELECT id FROM seats WHERE event_id = %s AND seat_no = %s", (event_id, seat_no)
    ).fetchone()[0]


def hold(conn: psycopg.Connection, event_id, entry_id: int, seat: int, status: str = "HELD") -> int:
    return conn.execute(
        """
        INSERT INTO allocations (event_id, entry_id, seat_id, status, held_at, hold_expires_at,
                                 confirmed_at, ticket_code)
        VALUES (%s, %s, %s, %s, fd_now(), fd_now() + interval '5 minutes',
                CASE WHEN %s = 'CONFIRMED' THEN fd_now() END,
                CASE WHEN %s = 'CONFIRMED' THEN gen_random_uuid()::text END)
        RETURNING id
        """,
        (event_id, entry_id, seat, status, status, status),
    ).fetchone()[0]


# ------------------------------------------------------------------ API client
ADMIN = {"X-Admin-Token": get_settings().admin_token}


@pytest_asyncio.fixture
async def api(db) -> AsyncIterator[httpx.AsyncClient]:
    """In-process ASGI client wired to the test database (fresh tables per test)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import set_engine
    from app.main import create_app

    engine = create_async_engine(get_settings().test_database_url, pool_size=50, max_overflow=20)
    set_engine(engine)
    # raise_app_exceptions=False: assert on the 500 envelope like a real client would.
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as client:
        yield client
    hooks.reset_entry_gate()
    set_engine(None)
    await engine.dispose()


def iso(dt: datetime) -> str:
    return dt.isoformat()


async def create_event(api: httpx.AsyncClient, *, inventory: int = 5, mode: str = "LOTTERY",
                       opens_in: float = -60, closes_in: float = 600, ttl: int = 60,
                       schedule: bool = True, config: dict | None = None) -> dict:
    """Create (and by default schedule) an event; times relative to now in seconds."""
    now = datetime.now(UTC)
    r = await api.post("/admin/events", headers=ADMIN, json={
        "name": "t", "inventory": inventory, "mode": mode,
        "window_opens_at": iso(now + timedelta(seconds=opens_in)),
        "window_closes_at": iso(now + timedelta(seconds=closes_in)),
        "claim_ttl_seconds": ttl, "config": config or {},
    })
    assert r.status_code == 201, r.text
    ev = r.json()
    if schedule:
        r = await api.post(f"/admin/events/{ev['id']}/schedule", headers=ADMIN)
        assert r.status_code == 200, r.text
        ev = r.json()["event"]
    return ev


def make_users(conn: psycopg.Connection, n: int) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(n)]
    with conn.cursor() as cur, cur.copy("COPY users (id, email) FROM STDIN") as cp:
        for u in ids:
            cp.write_row((u, f"{u}@test.local"))
    return ids
