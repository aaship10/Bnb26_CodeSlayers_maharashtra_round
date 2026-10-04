"""Wires Member B's defence package into this app. Enable with PLUGINS=app.defence_plugin.

What it installs: client-IP and metrics middleware, B's error handling for B's own ApiError, the
/admin/defence/*, /defence/* and /metrics routes, B's connection lifecycle (migrations, Postgres
pool, Redis) around the app lifespan, and the entry gate (adapted to this app's hook types).

What it deliberately does NOT install: B's /auth/* routes. This app already has its own email
verification (app/routers/auth.py); two routers on the same paths would shadow each other.
Per-endpoint rate limits are applied by app.hooks.rate_limit, which routes call as a dependency.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI


def _export_env() -> None:
    """B reads os.environ only; this app reads backend/.env through pydantic. Bridge the gap."""
    from app.config import get_settings, sync_url

    for line in (Path(__file__).resolve().parents[1] / ".env").read_text(encoding="utf-8").splitlines() \
            if (Path(__file__).resolve().parents[1] / ".env").exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split(" #", 1)[0].strip())
    s = get_settings()
    os.environ.setdefault("DATABASE_URL", sync_url(s.database_url))
    os.environ["DATABASE_URL"] = sync_url(os.environ["DATABASE_URL"])
    os.environ.setdefault("ADMIN_TOKEN", s.admin_token)
    os.environ.setdefault("AUTH_MODE", s.auth_mode)


def _adapt_gate():
    from app import hooks as A
    from app.defence import contracts as B
    from app.defence.gate import entry_gate as b_gate

    async def gate(ctx: A.GateContext) -> A.GateDecision:
        user = {"id": ctx.user.id, "email": ctx.user.email, "display_name": ctx.user.display_name}
        event = {"id": ctx.event.id, "config": ctx.event.config}
        d = await b_gate(B.GateContext(user=user, event=event, request=ctx.request,
                                       server_now=ctx.server_now, already_entered=ctx.already_entered))
        if d.action is B.Action.ALLOW:
            return A.GateDecision(A.GateAction.ALLOW, d.weight, d.reason, d.risk or {})
        if d.action is B.Action.CHALLENGE:
            return A.GateDecision(A.GateAction.CHALLENGE, 0.0, d.reason, d.risk or {},
                                  challenge=d.challenge.to_dict())
        return A.GateDecision(A.GateAction.REJECT, 0.0, d.reason, d.risk or {})

    return gate


def setup(app: FastAPI) -> None:
    _export_env()
    from app import hooks
    from app.defence import runtime
    from app.defence.clientip import ClientIPMiddleware
    from app.defence.errors import ApiError as BApiError, _resp
    from app.defence.metrics import MetricsMiddleware
    from app.defence.ratelimit.gate import rate_limit
    from app.defence.router import admin_router, metrics_router, public_router

    @app.exception_handler(BApiError)
    async def _b_api_error(_, exc: BApiError):
        return _resp(exc)

    app.add_middleware(MetricsMiddleware)
    app.add_middleware(ClientIPMiddleware)
    app.include_router(admin_router)
    app.include_router(public_router)
    app.include_router(metrics_router)

    from app.defence import lifecycle
    from app.db import get_engine
    from app.errors import ApiError as AApiError, ErrorCode as AErrorCode
    from sqlalchemy import text

    @app.get("/readyz", tags=["health"])
    async def readyz() -> dict:
        """503 while draining or when the database is unreachable (B's gateway and chaos runs poll it)."""
        if lifecycle.is_draining():
            raise AApiError(AErrorCode.INTERNAL, "draining", status=503)
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            raise AApiError(AErrorCode.INTERNAL, "database unavailable", status=503) from None
        return {"status": "ready"}

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        async with original(a) as state:
            await runtime.startup()
            try:
                yield state
            finally:
                await runtime.shutdown()

    app.router.lifespan_context = lifespan
    hooks.register_entry_gate(_adapt_gate())
    hooks.register_rate_limit(rate_limit)

    from app.defence.config import DefenceConfigError, validate_defences
    from app.defence.presets import list_presets

    def validator(blob):
        try:
            return validate_defences(blob).model_dump(mode="json")
        except DefenceConfigError as exc:
            raise ValueError(exc.errors) from None

    hooks.register_config_hooks(validator, list_presets)
