"""FastAPI application factory."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import dispose_engine
from app.errors import install_error_handlers
from app.plugins import load_plugins
from app.routers import admin, auth, events, fairness


@asynccontextmanager
async def lifespan(_: FastAPI):
    worker = None
    if get_settings().run_worker:        # single-process demos; otherwise run `python -m app.worker`
        from app.worker import run_forever
        worker = asyncio.create_task(run_forever())
    yield
    if worker is not None:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Fair Drop Core API",
        version="0.2.0",
        description="Time-window lottery with a provably fair draw. Errors: {code, message, details?}.",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(events.router)
    app.include_router(fairness.router)
    app.include_router(admin.router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict:
        # Liveness only; /readyz (stage 6) also checks the database.
        return {"status": "ok"}

    load_plugins(app, get_settings().plugins)

    import os
    from fastapi.staticfiles import StaticFiles

    frontend_dist = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../frontend/dist"))
    if os.path.exists(frontend_dist):
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


app = create_app()
