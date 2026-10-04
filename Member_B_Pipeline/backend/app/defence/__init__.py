"""Fair Drop defence package (owned by Member B).

A wires it with exactly two lines:

    from app.defence import install
    install(app)                       # in the FastAPI app factory

    from app.defence.gate import entry_gate   # in app/hooks.py

and replaces the body of get_current_user with
app.defence.identity.deps.get_current_user.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .clientip import ClientIPMiddleware
from .errors import install_error_handlers
from .metrics import MetricsMiddleware
from .router import admin_router, metrics_router, public_router

__all__ = ["install"]


def install(app: FastAPI) -> None:
    """Mount everything the defence package contributes to the app.
    Later stages add rate-limit middleware, challenge routes and metrics here."""
    from .identity.routes import router as auth_router
    from .identity.routes import sim_router
    from . import runtime

    install_error_handlers(app)
    app.add_middleware(MetricsMiddleware)  # request count + latency by route template
    app.add_middleware(ClientIPMiddleware)  # sets request.state.client_ip (trusted-proxy rules)
    app.include_router(admin_router)
    app.include_router(public_router)
    app.include_router(metrics_router)
    app.include_router(auth_router)
    app.include_router(sim_router)

    # Wrap (not replace) whatever lifespan the host app already has: our migrations and
    # connection pool open after it starts and close before it stops.
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
