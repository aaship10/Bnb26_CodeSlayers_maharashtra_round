"""FastAPI wrapper around the in-memory mock (routes have no /api prefix, like A's backend).

Run:  fdsim mock --port 8200      (or: python -m mock_server)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core import Event, MockError, Store, iso, parse_iso
from .defences import presets_view


def _precise_wall() -> Callable[[], float]:
    """Wall clock with perf_counter resolution. time.time() ticks every 15.6 ms on
    Windows, which would give simultaneous entries identical timestamps."""
    w0, p0 = time.time(), time.perf_counter()
    return lambda: w0 + (time.perf_counter() - p0)


class Clock:
    """Wall clock by default; tests swap in a manual one."""

    def __init__(self, fn: Callable[[], float] | None = None):
        self._fn = fn or _precise_wall()

    def now(self) -> float:
        return self._fn()


class ManualClock(Clock):
    def __init__(self, start: float = 1_793_534_400.0):  # 2026-11-01T10:00:00Z
        self.t = start
        super().__init__(lambda: self.t)

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    auth_mode: str = field(default_factory=lambda: os.environ.get("AUTH_MODE", "dev"))
    simulation_mode: bool = field(default_factory=lambda: _env_bool("SIMULATION_MODE", True))
    sim_key: str = field(default_factory=lambda: os.environ.get("SIM_KEY", "dev-sim-key"))
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", "dev-admin-token"))
    tick_interval_s: float = 0.25


def create_app(settings: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    settings = settings or Settings()
    clock = clock or Clock()
    store = Store()
    counters: Counter[str] = Counter()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        async def ticker() -> None:
            while True:
                await asyncio.sleep(settings.tick_interval_s)
                store.tick_all(clock.now())

        task = asyncio.create_task(ticker())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Fair Drop MOCK target (Member C dev double)", version="1", lifespan=lifespan)
    app.state.store = store
    app.state.clock = clock
    app.state.settings = settings

    # ------------------------------------------------------------- errors

    def error_response(status: int, code: str, message: str, details: Any = None,
                       headers: dict[str, str] | None = None) -> JSONResponse:
        body: dict[str, Any] = {"code": code, "message": message}
        if details is not None:
            body["details"] = details
        counters[f"error:{code}"] += 1
        return JSONResponse(body, status_code=status, headers=headers)

    @app.exception_handler(MockError)
    async def _mock_error(_: Request, e: MockError) -> JSONResponse:
        return error_response(e.status, e.code, e.message, e.details, e.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError) -> JSONResponse:
        return error_response(400, "VALIDATION_ERROR", "Invalid request", {"errors": e.errors()})

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 401: "UNAUTHENTICATED", 403: "FORBIDDEN"}.get(e.status_code, "VALIDATION_ERROR")
        return error_response(e.status_code, code, str(e.detail))

    # ------------------------------------------------------------- identity

    def sim_key_ok(req: Request) -> bool:
        key = req.headers.get("x-sim-key")
        if key is None:
            return False
        if not settings.simulation_mode or not hmac.compare_digest(key, settings.sim_key):
            raise MockError(403, "FORBIDDEN", "Invalid simulation key")
        return True

    def mint_token(user_id: str) -> str:
        mac = hmac.new(settings.admin_token.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:16]
        return f"mjwt.{user_id}.{mac}"

    def authenticate(req: Request) -> str:
        auth = req.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            parts = auth[7:].split(".")
            if len(parts) == 3 and parts[0] == "mjwt" and hmac.compare_digest(mint_token(parts[1]), auth[7:]):
                return parts[1]
            raise MockError(401, "UNAUTHENTICATED", "Invalid token")
        uid = req.headers.get("x-user-id")
        if uid:
            if settings.auth_mode == "dev" or sim_key_ok(req):
                if len(uid) > 64:
                    raise MockError(400, "VALIDATION_ERROR", "X-User-Id too long")
                return uid
        raise MockError(401, "UNAUTHENTICATED", "Sign in required")

    def client_ip(req: Request) -> str:
        fake = req.headers.get("x-sim-client-ip")
        if fake and sim_key_ok(req):
            return fake
        return req.client.host if req.client else "0.0.0.0"

    def require_admin(req: Request) -> None:
        tok = req.headers.get("x-admin-token")
        if tok is None:
            raise MockError(401, "UNAUTHENTICATED", "X-Admin-Token required")
        if not hmac.compare_digest(tok, settings.admin_token):
            raise MockError(403, "FORBIDDEN", "Bad admin token")

    async def body_of(req: Request) -> dict[str, Any]:
        raw = await req.body()
        if not raw:
            return {}
        try:
            data = await req.json()
        except ValueError:
            raise MockError(400, "VALIDATION_ERROR", "Body must be JSON") from None
        if not isinstance(data, dict):
            raise MockError(400, "VALIDATION_ERROR", "Body must be a JSON object")
        return data

    # ------------------------------------------------------------- views

    def event_view(ev: Event) -> dict[str, Any]:
        now = clock.now()
        store.tick(ev, now)
        return {
            "id": ev.id,
            "name": ev.name,
            "description": ev.description,
            "phase": ev.phase,
            "mode": ev.mode,
            "inventory": ev.inventory,
            "window_opens_at": iso(ev.opens_at),
            "window_closes_at": iso(ev.closes_at),
            "window_seconds": ev.window_seconds,
            "claim_ttl_s": ev.claim_ttl_s,
            "seed_commitment": ev.commitment,
            "server_seed_hex": ev.server_seed_hex if ev.drawn_at is not None else None,
            "config": {"defences": ev.defences},
            "mock": True,
            "server_now": iso(now),
        }

    def stats_view(ev: Event) -> dict[str, Any]:
        store.tick(ev, clock.now())
        states = Counter(e.state for e in ev.entries.values())
        return {
            "event_id": ev.id,
            "phase": ev.phase,
            "entries": len(ev.entries),
            "states": dict(states),
            "held": ev.held,
            "confirmed": ev.confirmed,
            "remaining": ev.inventory - ev.held - ev.confirmed,
            "decisions": len(ev.defence_state.decisions),
            "requests": {k: v for k, v in counters.items()},
            "server_now": iso(clock.now()),
        }

    # ------------------------------------------------------------- public

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "mock": True, "server_now": iso(clock.now())}

    @app.get("/events")
    async def list_events() -> list[dict[str, Any]]:
        return [event_view(ev) for ev in store.events.values()]

    @app.get("/events/{event_id}")
    async def get_event(event_id: str) -> dict[str, Any]:
        return event_view(store.get(event_id))

    @app.post("/events/{event_id}/enter")
    async def enter(event_id: str, req: Request) -> dict[str, Any]:
        counters["enter"] += 1
        uid = authenticate(req)
        ev = store.get(event_id)
        now = clock.now()
        ip = client_ip(req)
        ds = ev.defence_state
        ds.rate_limit(ev, now, uid, ip)
        device = req.headers.get("x-device-id")
        ds.observe(ev, now, uid, device)
        if uid not in ev.entries:  # an idempotent replay needs no new challenge
            store.tick(ev, now)
            if ev.phase == "OPEN":
                ds.check(ev, "enter", uid, now, req.headers.get("x-challenge-id"),
                         req.headers.get("x-challenge-solution"))
        e, already = store.enter(ev, uid, now, ip, device)
        return {"state": e.state, "entered_at": iso(e.entered_at), "already_entered": already, "server_now": iso(now)}

    @app.get("/events/{event_id}/status")
    async def status(event_id: str, req: Request) -> dict[str, Any]:
        counters["status"] += 1
        uid = authenticate(req)
        ev = store.get(event_id)
        now = clock.now()
        ev.defence_state.rate_limit(ev, now, uid, client_ip(req))
        return {**store.status(ev, uid, now), "server_now": iso(now)}

    @app.post("/events/{event_id}/claim")
    async def claim(event_id: str, req: Request) -> dict[str, Any]:
        counters["claim"] += 1
        uid = authenticate(req)
        ev = store.get(event_id)
        now = clock.now()
        ev.defence_state.rate_limit(ev, now, uid, client_ip(req))
        key = req.headers.get("idempotency-key", "")
        replay = ev.idempotency.get((uid, key))
        if replay is None:
            ev.defence_state.check(ev, "claim", uid, now, req.headers.get("x-challenge-id"),
                                   req.headers.get("x-challenge-solution"))
        return {**store.claim(ev, uid, key, now), "server_now": iso(now)}

    @app.post("/defence/challenge")
    async def challenge(req: Request) -> dict[str, Any]:
        uid = authenticate(req)
        b = await body_of(req)
        ev = store.get(str(b.get("event_id", "")))
        kind = ev.defence_state.required(ev, "enter", uid) or "pow"
        return ev.defence_state.issue(ev, kind, uid, clock.now())

    # ------------------------------------------------------------- admin

    @app.post("/admin/events")
    async def admin_create(req: Request) -> dict[str, Any]:
        require_admin(req)
        b = await body_of(req)
        try:
            ev = store.create_event(
                clock.now(),
                name=str(b.get("name", "Mock drop")),
                description=str(b.get("description", "")),
                inventory=int(b.get("inventory", 500)),
                mode=str(b.get("mode", "LOTTERY")),
                window_seconds=float(b.get("window_seconds", 60)),
                claim_ttl_seconds=float(b.get("claim_ttl_seconds", b.get("claim_ttl_s", 60))),
                event_id=b.get("id"),
                server_seed_hex=b.get("server_seed_hex"),
                defences=(b.get("config") or {}).get("defences"),
            )
        except (TypeError, ValueError) as e:
            raise MockError(400, "VALIDATION_ERROR", str(e)) from None
        return event_view(ev)

    @app.post("/admin/events/{event_id}/schedule")
    async def admin_schedule(event_id: str, req: Request) -> dict[str, Any]:
        require_admin(req)
        b = await body_of(req)
        ev = store.get(event_id)
        now = clock.now()
        opens = parse_iso(b["opens_at"]) if "opens_at" in b else now + float(b.get("opens_in_s", 0))
        store.schedule(ev, now, opens, float(b["window_seconds"]) if "window_seconds" in b else None)
        return event_view(ev)

    @app.post("/admin/events/{event_id}/open")
    async def admin_open(event_id: str, req: Request) -> dict[str, Any]:
        require_admin(req)
        ev = store.get(event_id)
        store.open(ev, clock.now())
        return event_view(ev)

    @app.post("/admin/events/{event_id}/close")
    async def admin_close(event_id: str, req: Request) -> dict[str, Any]:
        require_admin(req)
        ev = store.get(event_id)
        store.close(ev, clock.now())
        return event_view(ev)

    @app.post("/admin/events/{event_id}/draw")
    async def admin_draw(event_id: str, req: Request) -> dict[str, Any]:
        require_admin(req)
        b = await body_of(req)
        ev = store.get(event_id)
        summary = store.draw(ev, clock.now(), b.get("server_seed_hex"))
        return {**event_view(ev), "draw": summary}

    @app.patch("/admin/events/{event_id}/config")
    async def admin_config(event_id: str, req: Request) -> dict[str, Any]:
        require_admin(req)
        b = await body_of(req)
        ev = store.get(event_id)
        defences = b.get("defences", (b.get("config") or {}).get("defences"))
        if not isinstance(defences, dict):
            raise MockError(400, "VALIDATION_ERROR", "defences object required", {"field": "defences"})
        store.set_config(ev, defences)
        return event_view(ev)

    @app.get("/admin/events/{event_id}/stats")
    async def admin_stats(event_id: str, req: Request) -> dict[str, Any]:
        require_admin(req)
        return stats_view(store.get(event_id))

    @app.get("/admin/events/{event_id}/invariants")
    async def admin_invariants(event_id: str, req: Request) -> dict[str, Any]:
        require_admin(req)
        ev = store.get(event_id)
        return {**store.invariants(ev, clock.now()), "event_id": ev.id, "server_now": iso(clock.now())}

    @app.post("/admin/events/{event_id}/reset")
    async def admin_reset(event_id: str, req: Request) -> dict[str, Any]:
        require_admin(req)
        b = await body_of(req)
        store.reset(store.get(event_id), b.get("server_seed_hex"))
        return event_view(store.get(event_id))

    @app.get("/admin/defence/presets")
    async def admin_presets(req: Request) -> list[dict[str, Any]]:
        require_admin(req)
        return presets_view()

    @app.get("/admin/defence/decisions")
    async def admin_decisions(req: Request, event_id: str) -> list[dict[str, Any]]:
        require_admin(req)
        return [{"event_id": event_id, **d} for d in store.get(event_id).defence_state.decisions]

    @app.post("/admin/sim/tokens")
    async def admin_tokens(req: Request) -> dict[str, Any]:
        require_admin(req)
        if not settings.simulation_mode:
            raise MockError(403, "FORBIDDEN", "SIMULATION_MODE is off")
        b = await body_of(req)
        ids = b.get("user_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
            raise MockError(400, "VALIDATION_ERROR", "user_ids must be a list of strings", {"field": "user_ids"})
        return {"tokens": {uid: mint_token(uid) for uid in ids}}

    # ------------------------------------------------------------- mock-only analytics

    @app.get("/__mock/events/{event_id}/export")
    async def mock_export(event_id: str, req: Request) -> dict[str, Any]:
        """What db_adapter reads from Postgres on the real target. Contains NO sim_label:
        ground truth lives only in the simulator."""
        require_admin(req)
        ev = store.get(event_id)
        store.tick(ev, clock.now())
        return {
            "event": event_view(ev),
            "drawn_at": iso(ev.drawn_at),
            "entries": [
                {
                    "user_id": e.user_id, "arrival_seq": e.arrival_seq, "entered_at": iso(e.entered_at),
                    "state": e.state, "draw_state": e.draw_state, "weight": e.weight, "risk": e.risk,
                    "client_ip": e.client_ip, "device_id": e.device_id, "queue_index": e.queue_index,
                    "promoted": e.promoted, "seat_no": e.seat_no, "claimed_at": iso(e.claimed_at),
                }
                for e in ev.entries.values()
            ],
            "decisions": ev.defence_state.decisions,
        }

    @app.get("/__mock/events/{event_id}/verify")
    async def mock_verify(event_id: str, req: Request) -> dict[str, Any]:
        require_admin(req)
        return store.verify_draw(store.get(event_id))

    return app
