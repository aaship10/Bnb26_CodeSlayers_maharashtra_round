"""Test wiring.

Import path: with A's backend present (backend/app/__init__.py) use it; otherwise the
stub's `app` package grafts backend/app onto itself (see infra/stub_api/app/__init__.py).

Integration tests need Postgres. No Docker/testcontainers: they use the private local
cluster (`python infra/local/pg.py start`, database <POSTGRES_DB>_test) or
FD_TEST_DATABASE_URL. If it is unreachable they SKIP with an explicit reason, never
silently pass. Redis: a real server if FD_TEST_REDIS_URL answers, otherwise fakeredis
(with Lua); `redis_kind` tells tests (and the report header) which one ran.
"""
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
STUB = ROOT / "infra" / "stub_api"

if (BACKEND / "app" / "__init__.py").exists():
    sys.path.insert(0, str(BACKEND))
else:
    sys.path.insert(0, str(STUB))
    import app  # noqa: E402,F401  (stub package; grafts backend/app itself)

ENV_KEYS = (
    "ADMIN_TOKEN", "AUTH_MODE", "SIMULATION_MODE", "SIM_KEY", "TRUSTED_PROXIES", "JWT_SECRET", "OTP_PEPPER",
    "DATABASE_URL", "REDIS_URL", "ALLOWED_EMAIL_DOMAINS", "SMTP_HOST", "JWT_TTL_S", "JWT_REFRESH_GRACE_S",
    "JWT_SESSION_MAX_S", "DEFENCE_AUTO_MIGRATE", "FD_ENV", "OUTBOX_DIR", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_TLS", "MAIL_FROM", "CAPTCHA_SECRET", "POW_SECRET", "MOCK_CAPTCHA_UI",
)
TEST_JWT_SECRET = "t" * 40


def pytest_asyncio_loop_factories(config, item):
    """psycopg's async mode cannot run on Windows' default ProactorEventLoop. Use a selector
    loop everywhere (identical behaviour on Linux/macOS). Needs pytest-asyncio >= 1.4."""
    import asyncio
    import selectors

    return {"selector": lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())}


def pytest_report_header(config):
    return f"fairdrop-b: postgres={_test_dsn() and 'configured'} redis={os.environ.get('FD_TEST_REDIS_URL', 'fakeredis unless local redis found')}"


def _test_redis_url() -> str | None:
    """FD_TEST_REDIS_URL, forced onto database 15 unless the URL names another non-zero database.
    The tests flushdb(): doing that on database 0 would wipe the live stack's rate-limit state."""
    url = os.environ.get("FD_TEST_REDIS_URL")
    if not url:
        return None
    from urllib.parse import urlparse, urlunparse

    u = urlparse(url)
    if u.path in ("", "/", "/0"):
        u = u._replace(path="/15")
    return urlunparse(u)


def _test_dsn() -> str | None:
    if os.environ.get("FD_TEST_DATABASE_URL"):
        return os.environ["FD_TEST_DATABASE_URL"]
    env_file = ROOT / "infra" / ".env"
    if not env_file.exists():
        return None
    kv = dict(
        line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    try:
        return (
            f"postgresql://{kv['POSTGRES_USER']}:{kv['POSTGRES_PASSWORD']}@127.0.0.1:"
            f"{kv['POSTGRES_PORT']}/{kv['POSTGRES_DB']}_test"
        )
    except KeyError:
        return None


@pytest.fixture
def settings_env(monkeypatch):
    """Set env vars and drop the cached Settings so each test sees its own."""
    from app.defence.settings import get_settings

    def apply(**env: str) -> None:
        for k in ENV_KEYS:
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


# --------------------------------------------------------------- integration
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pg_dsn():
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("no test database configured: run `python infra/local/run.py up` or set FD_TEST_DATABASE_URL")
    import psycopg

    try:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True, connect_timeout=3) as conn:
            await conn.execute("DROP SCHEMA IF EXISTS defence CASCADE")
            await conn.execute("DROP TABLE IF EXISTS public.entries, public.events, public.users CASCADE")
            await conn.execute(
                """CREATE TABLE public.users (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                       email text NOT NULL UNIQUE, display_name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"""
            )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"test Postgres unreachable ({exc!r}); start it with `python infra/local/pg.py start`")
    from app.defence.migrate import upgrade

    import asyncio

    await asyncio.to_thread(upgrade, dsn)
    return dsn


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def redis_kind():
    url = _test_redis_url()
    if url:
        import redis.asyncio as aioredis

        try:
            await aioredis.from_url(url, protocol=2).ping()
            return "real"
        except Exception:  # noqa: BLE001
            pass
    return "fake"


@pytest_asyncio.fixture(loop_scope="session")
async def redis_client(redis_kind):
    if redis_kind == "real":
        import redis.asyncio as aioredis

        from app.defence.runtime import make_redis

        r = make_redis(_test_redis_url())
    else:
        import fakeredis

        r = fakeredis.FakeAsyncRedis(decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


class CapturingMailer:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    def last_otp(self, to: str | None = None) -> str:
        import re

        for m in reversed(self.sent):
            if to is None or m.to == to:
                return re.search(r"\b(\d{6})\b", m.body).group(1)
        raise AssertionError("no mail sent")


@pytest_asyncio.fixture(loop_scope="session")
async def rt(pg_dsn, redis_client, settings_env):
    """A defence Runtime wired to the test Postgres, Redis and a capturing mailer."""
    from app.defence import runtime
    from app.defence.ratelimit.bucket import RedisLimiter
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    settings_env(
        FD_ENV="dev", DATABASE_URL=pg_dsn, JWT_SECRET=TEST_JWT_SECRET, ADMIN_TOKEN="adm", AUTH_MODE="jwt",
        TRUSTED_PROXIES="127.0.0.1/32", ALLOWED_EMAIL_DOMAINS="example-college.edu,gmail.com",
    )
    pool = AsyncConnectionPool(pg_dsn, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=False)
    await pool.open(wait=True, timeout=10)
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE defence.identities, defence.pending_registrations, defence.decisions, defence.waivers, public.users CASCADE")
    from app.defence.decisionlog.writer import DecisionLog
    from app.defence.signals import counts

    counts._inflight.clear()
    mailer = CapturingMailer()
    log = DecisionLog(pool, interval_s=0.05)
    log.start()
    runtime._rt = runtime.Runtime(pg=pool, redis=redis_client, limiter=RedisLimiter(redis_client, "test"), mailer=mailer, decisions=log)
    yield runtime._rt
    runtime._rt = None
    await log.stop()
    await pool.close()


@pytest_asyncio.fixture(loop_scope="session")
async def client(rt):
    """httpx client against the stub app (which installs the defence package)."""
    import httpx
    from app.main import app

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# -------------------------------------------------- stub schema + event + user factory (stages 3+)
EV = "11111111-1111-1111-1111-111111111111"


@pytest_asyncio.fixture(loop_scope="session")
async def stub(rt, client):
    """Stub schema + event, a helper to set the event's defences, and a user factory."""
    import json
    import uuid

    from app import db
    from app.defence import config_source
    from app.defence.config import validate_defences
    from app.defence.identity import tokens

    await db.open_pool()
    await db.init_schema()
    async with rt.pg.connection() as conn:
        await conn.execute("DELETE FROM entries")
        await conn.execute("UPDATE events SET config = '{}'::jsonb WHERE id = %s", (EV,))
    config_source.clear_cache()

    class Stub:
        async def set_defences(self, blob):
            cfg = validate_defences(blob).model_dump(mode="json")
            async with rt.pg.connection() as conn:
                await conn.execute("UPDATE events SET config = %s::jsonb WHERE id = %s", (json.dumps({"defences": cfg}), EV))
            config_source.clear_cache()

        async def user(self):
            uid = uuid.uuid4()
            async with rt.pg.connection() as conn:
                await conn.execute("INSERT INTO public.users (id, email, display_name) VALUES (%s, %s, 'T')", (uid, f"{uid}@example-college.edu"))
            tok, _ = tokens.mint(uid)
            return uid, {"Authorization": f"Bearer {tok}"}

    yield Stub()
    await db.close_pool()
    config_source.clear_cache()


