"""The /sim HTTP service (port 8100). D's panel drives this.

Routes are mounted under /sim and at the root, so it works whether nginx strips the prefix
(`/sim/runs` -> `/runs`) or forwards it as is (D's Vite dev proxy does).

  GET  /scenarios                    POST /runs {scenario_id, overrides, repeats, seed, target}
  GET  /runs                         GET  /runs/{id}          POST /runs/{id}/cancel
  GET  /runs/{id}/results            GET  /runs/{id}/stream   (SSE, Last-Event-ID resume)
  GET  /experiments                  GET  /experiments/{id}
  GET  /charts/{chart_id}?experiment=   and   /charts/{chart_id}.png?experiment=

Errors are {code, message, details?}. 409 TARGET_UNAVAILABLE carries a sentence a UI can
show as-is (C4).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fairdrop_sim.service import catalogue as cat
from fairdrop_sim.service.runs import TERMINAL, Run, RunManager, ScenarioUnavailable
from fairdrop_sim.service.store import ChartStore
from fairdrop_sim.service.targets import Targets, TargetUnavailable

REPO_ROOT = Path(__file__).resolve().parents[3]
SIM_ROOT = REPO_ROOT / "simulator"
HEARTBEAT_S = 15.0


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def create_app(results_dir: Path | None = None, include_samples: bool | None = None,
               heartbeat_s: float = HEARTBEAT_S) -> FastAPI:
    results_dir = Path(os.environ.get("SIM_RESULTS_DIR", results_dir or SIM_ROOT / "results"))
    if include_samples is None:
        include_samples = os.environ.get("SIM_INCLUDE_SAMPLES", "1") not in ("0", "false", "no")
    targets = Targets()
    manager = RunManager(targets, results_dir / "service")
    store = ChartStore(results_dir / "experiments", REPO_ROOT / "docs" / "sample_results", include_samples)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(title="Fair Drop simulator service (Member C)", version="1", lifespan=lifespan)
    app.state.manager, app.state.targets, app.state.store = manager, targets, store
    router = APIRouter()

    # ------------------------------------------------------------------ errors

    def err(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
        body: dict[str, Any] = {"code": code, "message": message}
        if details:
            body["details"] = details
        return JSONResponse(body, status_code=status)

    @app.exception_handler(ApiError)
    async def _api(_: Request, e: ApiError) -> JSONResponse:
        return err(e.status, e.code, e.message, e.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError) -> JSONResponse:
        return err(400, "VALIDATION_ERROR", "Invalid request", {"errors": json.loads(json.dumps(e.errors(), default=str))})

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException) -> JSONResponse:
        return err(e.status_code, "NOT_FOUND" if e.status_code == 404 else "VALIDATION_ERROR", str(e.detail))

    def get_run(run_id: str) -> Run:
        run = manager.runs.get(run_id)
        if run is None:
            raise ApiError(404, "NOT_FOUND", "No such run")
        return run

    # ------------------------------------------------------------------ routes

    @router.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "fairdrop-sim", "runs": len(manager.runs)}

    @router.get("/scenarios")
    async def scenarios() -> list[dict[str, Any]]:
        return [cat.public_view(s) for s in cat.SCENARIOS]

    @router.post("/runs", status_code=201)
    async def start_run(request: Request) -> dict[str, str]:
        try:
            body = await request.json()
        except ValueError:
            raise ApiError(400, "VALIDATION_ERROR", "Body must be JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "VALIDATION_ERROR", "Body must be a JSON object")
        sid = body.get("scenario_id")
        if sid not in cat.BY_ID:
            raise ApiError(400, "VALIDATION_ERROR", f"Unknown scenario: {sid}", {"field": "scenario_id"})
        target = body.get("target")
        if target not in ("mock", "real"):
            raise ApiError(400, "VALIDATION_ERROR", 'target must be "mock" or "real"', {"field": "target"})
        repeats = body.get("repeats", 5)
        if not isinstance(repeats, int) or isinstance(repeats, bool) or not 1 <= repeats <= 50:
            raise ApiError(400, "VALIDATION_ERROR", "repeats must be a whole number from 1 to 50", {"field": "repeats"})
        seed = body.get("seed", 42)
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ApiError(400, "VALIDATION_ERROR", "seed must be a non-negative integer", {"field": "seed"})
        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ApiError(400, "VALIDATION_ERROR", "overrides must be an object", {"field": "overrides"})
        try:
            run = await manager.submit(sid, overrides, repeats, seed, target)
        except cat.ParamError as e:
            raise ApiError(400, "VALIDATION_ERROR", str(e), {"field": "overrides"}) from None
        except ScenarioUnavailable as e:
            raise ApiError(409, "SCENARIO_UNAVAILABLE", str(e), {"field": "scenario_id"}) from None
        except TargetUnavailable as e:
            raise ApiError(409, "TARGET_UNAVAILABLE", str(e), {"field": "target"}) from None
        return {"run_id": run.id}

    @router.get("/runs")
    async def list_runs() -> list[dict[str, Any]]:
        return [r.summary() for r in manager.list()]

    @router.get("/runs/{run_id}")
    async def run_status(run_id: str) -> dict[str, Any]:
        return get_run(run_id).view()

    @router.get("/runs/{run_id}/results")
    async def run_results(run_id: str) -> dict[str, Any]:
        run = get_run(run_id)
        if run.results is None:
            raise ApiError(409, "VALIDATION_ERROR",
                           f"Results are available when the run is done (it is {run.status})")
        return run.results

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str) -> dict[str, Any]:
        run = get_run(run_id)
        await manager.cancel(run)
        return run.view()

    @router.get("/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request) -> StreamingResponse:
        run = get_run(run_id)
        raw = request.headers.get("last-event-id")
        last = int(raw) if raw and raw.isdigit() else None

        async def gen() -> AsyncIterator[str]:
            yield "retry: 2000\n\n"
            sent = last if last is not None else len(run.log)
            if last is None:  # fresh connection: the current status first, so the page is never blank
                yield f"id: {len(run.log)}\nevent: status\ndata: {json.dumps(run.view())}\n\n"
            while True:
                heartbeat = False
                async with run.cond:
                    # check and wait under the lock, so an event appended in between cannot be missed.
                    # Never yield while holding it: a suspended generator would block every emitter.
                    if sent >= len(run.log) and run.status not in TERMINAL:
                        try:
                            await asyncio.wait_for(run.cond.wait(), heartbeat_s)
                        except asyncio.TimeoutError:
                            heartbeat = True
                    batch = run.log[sent:]
                    done = run.status in TERMINAL
                if heartbeat and not batch:
                    yield ": hb\n\n"
                    continue
                for eid, ev, data in batch:
                    yield f"id: {eid}\nevent: {ev}\ndata: {data}\n\n"
                sent += len(batch)
                if done and sent >= len(run.log):
                    return

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @router.get("/experiments")
    async def experiments() -> list[dict[str, Any]]:
        return store.experiments()

    @router.get("/experiments/{exp_id}")
    async def experiment(exp_id: str) -> dict[str, Any]:
        e = store.experiment(exp_id)
        if e is None:
            raise ApiError(404, "NOT_FOUND", "No such experiment")
        return e

    @router.get("/charts/{file}")
    async def chart(file: str, experiment: str | None = None) -> Response:
        png = file.endswith(".png")
        chart_id = file[:-4] if png else file
        if png:
            data = await asyncio.get_running_loop().run_in_executor(None, store.png, chart_id, experiment)
            if data is None:
                raise ApiError(404, "NOT_FOUND", f"No such chart: {chart_id}")
            return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache"})
        found = store.chart(chart_id, experiment)
        if found is None:
            raise ApiError(404, "NOT_FOUND", f"No such chart: {chart_id}")
        return JSONResponse(found[0])

    app.include_router(router, prefix="/sim")  # as forwarded by D's dev proxy
    app.include_router(router)  # as forwarded by nginx with the prefix stripped
    return app
