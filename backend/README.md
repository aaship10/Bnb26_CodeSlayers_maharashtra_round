# Fair Drop: core allocation engine (Member A)

Time-window lottery with a provably fair draw and a claim phase. Integrity is
enforced by Postgres (constraints, partial unique indexes, guard triggers,
locks); the API and worker are stateless and safe to run as many replicas.

Status: **stage 2**: schema, admin lifecycle (create/schedule/open/close/patch), dev auth,
idempotent `/enter`, `/status`, `entry_gate` hook, plugin loading.

## Quick start (hosted Postgres, e.g. Neon)

Put the connection strings in `.env` (gitignored). Plain `postgresql://` URLs are fine.
Tests DROP and rebuild their schema, so `TEST_DATABASE_URL` must name a different
database; it is created automatically on the same server if missing.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
# .env: DATABASE_URL=postgresql://user:pw@host/neondb?sslmode=require
#       TEST_DATABASE_URL=postgresql://user:pw@host/fairdrop_test?sslmode=require
.venv\Scripts\alembic upgrade head
.venv\Scripts\python -m scripts.seed
.venv\Scripts\python -m pytest
```

## Quick start (native Postgres)

Requires Postgres 16+ installed locally (developed against 17 on port 5432) and Python 3.12.

```powershell
# one-time: create role fairdrop/fairdrop and databases fairdrop + fairdrop_test
& "C:\Program Files\PostgreSQL\17\bin\psql.exe" -U postgres -h localhost -p 5432 -f scripts/setup_local_db.sql

py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env                     # optional; edit if your Postgres uses another port
.venv\Scripts\alembic upgrade head         # create schema in the dev db
.venv\Scripts\python -m scripts.seed       # 50,000 users (10% sim_label=bot) + 3 demo events
.venv\Scripts\python -m pytest             # tests use the separate fairdrop_test db
```

Seed options: `--users N --bot-fraction F --seed S --reset`.

## Run the API

```powershell
.venv\Scripts\python -m app.serve --port 8000     # http://localhost:8000/docs
```

Use `app.serve` rather than the bare `uvicorn` CLI on Windows (psycopg async needs a
selector event loop; the launcher sets it up). Dev auth: `X-User-Id: <uuid>` or
`POST /auth/dev-login {"email": ...}` then `Authorization: Bearer <token>`. Admin:
`X-Admin-Token: dev-admin-token`.

## Extension points (Member B)

Set `PLUGINS=yourpkg.plugin`; its `setup(app)` can add middleware, override
`app.auth.get_current_user` via `app.dependency_overrides`, and call
`app.hooks.register_entry_gate(...)`. See `app/hooks.py` and `app/plugins.py`.

## Layout

```
app/          engine code (config, db, clock; routers/services arrive in later stages)
migrations/   Alembic, version table public.alembic_version (Member B uses schema "defence")
scripts/      seed.py (later: simulate_entries, concurrency_demo, demo_flow, export_openapi)
tests/        unit/ (no DB) and integration/ (real Postgres)
```

## Time

All deadlines use the SQL function `fd_now()` = transaction time + the offset in the
one-row `dev_clock` table. Tests and demos shift that offset (dev only) so every
replica sees the same fast-forwarded clock. Client clocks are never trusted.
