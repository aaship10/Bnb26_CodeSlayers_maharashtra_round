"""Single-origin dev gateway (stands in for nginx when nginx is not installed).

    /api/*  -> API replicas (prefix stripped), round-robin over .local/run/replicas
    /sim/*  -> simulator (Member C), prefix stripped
    else    -> built frontend (SPA fallback) or a placeholder page

Edge behaviour (the same ideas as infra/nginx/, which is the untested reference):
* X-Forwarded-For / X-Real-IP are OVERWRITTEN with the TCP peer; client values never reach the app.
* Per-IP edge limiter, deliberately GENEROUS (EDGE_BURST / EDGE_RPS): it only exists to stop
  absurd floods before they reach a replica; the precise limits are the app's. Requests with a
  valid simulation key bypass it so Member C's load generator is not throttled as one IP.
  Fine to keep in process memory: it is shed-load protection, not correctness.
* 1 s microcache for the public GETs (/events, /events/{id}). A cache hit gets a FRESH
  `server_now` patched in: clients derive their clock offset from it, and a second-old value
  would make every countdown a second wrong.
* Retry rules: a replica that refuses the connection is skipped (nothing was sent: safe for any
  method). A failure AFTER the request was sent is retried once ONLY for POST .../enter and
  .../claim (claim needs an Idempotency-Key): both are idempotent by contract. Nothing else is
  ever replayed, because replaying e.g. /auth/register could send two emails.
* SSE (/events/{id}/stream): no read timeout (heartbeats are 20 s apart), no retry, no buffering.
* /api/metrics is not exposed on the public origin.
Development tool: no TLS. Replicas must only be reachable from this gateway.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import re
import time
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[2]
REPLICAS_FILE = Path(os.environ.get("GATEWAY_REPLICAS_FILE", ROOT / ".local" / "run" / "replicas"))
SIMULATOR_URL = os.environ.get("SIMULATOR_URL", "http://127.0.0.1:8100").rstrip("/")


def _frontend_dir() -> Path:
    """FRONTEND_DIST, else the sibling monorepo's frontend/dist if it has been built, else a placeholder."""
    env = os.environ.get("FRONTEND_DIST")
    if env:
        return Path(env).resolve()
    sibling = ROOT.parent / "frontend" / "dist"
    return (sibling if (sibling / "index.html").is_file() else ROOT / "infra" / "nginx" / "placeholder").resolve()


FRONTEND = _frontend_dir()
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length",
}
MAX_BODY = 1_000_000
EDGE_BURST = float(os.environ.get("EDGE_BURST", "1500"))
EDGE_RPS = float(os.environ.get("EDGE_RPS", "400"))  # 0 disables the edge limiter
MICROCACHE_TTL_S = float(os.environ.get("MICROCACHE_TTL_S", "1.0"))
SIM_MODE = os.environ.get("SIMULATION_MODE", "false").lower() in ("1", "true", "yes", "on")
SIM_KEY = os.environ.get("SIM_KEY", "")

_PUBLIC_CACHEABLE = re.compile(r"^events(/[^/]+)?$")
_SSE = re.compile(r"^events/[^/]+/stream$")
_IDEMPOTENT_POST = re.compile(r"^events/[^/]+/(enter|claim)$")

_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=2.0), follow_redirects=False)
_sse_timeout = httpx.Timeout(connect=2.0, read=3600.0, write=15.0, pool=15.0)
_rr = itertools.count()
_cache: tuple[float, list[str]] = (-1.0, [])


def replicas() -> list[str]:
    global _cache
    try:
        mtime = REPLICAS_FILE.stat().st_mtime
    except OSError:
        return []
    if mtime != _cache[0]:
        _cache = (mtime, [f"http://{ln.strip()}" for ln in REPLICAS_FILE.read_text().splitlines() if ln.strip()])
    return _cache[1]


# -------------------------------------------------------------- replica health (drain support)
# Active: every HEALTH_INTERVAL_S (0.25 s) each replica's /readyz is polled; a draining or dead replica answers non-200
# and is taken out of rotation, so NEW requests stop going there while its in-flight ones finish.
# Passive: a refused connection marks the replica down for PASSIVE_DOWN_S immediately.
# Unknown (never checked, e.g. in tests) counts as ready; if NOTHING is ready we try everything rather than
# answering 503 from a stale view.
HEALTH_INTERVAL_S = float(os.environ.get("GATEWAY_HEALTH_INTERVAL_S", "0.25"))
PASSIVE_DOWN_S = 2.0
_ready: dict[str, bool] = {}
_down_until: dict[str, float] = {}


def routable(bases: list[str]) -> list[str]:
    now = time.monotonic()
    ok = [b for b in bases if _ready.get(b, True) and _down_until.get(b, 0.0) <= now]
    return ok or bases


async def health_loop() -> None:
    import asyncio

    while True:
        bases = replicas()
        results = await asyncio.gather(*[_probe(b) for b in bases], return_exceptions=True)
        for b, r in zip(bases, results):
            _ready[b] = r is True
        await asyncio.sleep(HEALTH_INTERVAL_S)


async def _probe(base: str) -> bool:
    try:
        r = await _client.get(f"{base}/readyz", timeout=httpx.Timeout(0.8))
        return r.status_code == 200
    except httpx.HTTPError:
        return False


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    task = asyncio.create_task(health_loop())
    try:
        yield
    finally:
        task.cancel()


# ----------------------------------------------------------------- edge limiter
_buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last_ts)


def edge_allow(ip: str, now: float | None = None) -> float:
    """0.0 if allowed, else seconds until a token is available."""
    if EDGE_RPS <= 0:
        return 0.0
    now = time.monotonic() if now is None else now
    tokens, ts = _buckets.get(ip, (EDGE_BURST, now))
    tokens = min(EDGE_BURST, tokens + (now - ts) * EDGE_RPS)
    if tokens < 1:
        _buckets[ip] = (tokens, now)
        return (1 - tokens) / EDGE_RPS
    _buckets[ip] = (tokens - 1, now)
    if len(_buckets) > 100_000:  # bound memory: drop buckets that have fully refilled
        for k in [k for k, (t, s) in _buckets.items() if t + (now - s) * EDGE_RPS >= EDGE_BURST][:50_000]:
            _buckets.pop(k, None)
    return 0.0


def _sim_bypass(request: Request) -> bool:
    import hmac

    key = request.headers.get("x-sim-key", "")
    return bool(SIM_MODE and SIM_KEY and key and hmac.compare_digest(key.encode(), SIM_KEY.encode()))


# ------------------------------------------------------------------ microcache
_mc: dict[str, tuple[float, int, dict[str, str], bytes]] = {}
_mc_locks: dict[str, asyncio.Lock] = {}


def _fresh_server_now(body: bytes) -> bytes:
    """Patch a current server_now into a cached JSON body (object or list of objects)."""
    from datetime import datetime, timezone

    try:
        data = json.loads(body)
    except ValueError:
        return body
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    for obj in data if isinstance(data, list) else [data]:
        if isinstance(obj, dict) and "server_now" in obj:
            obj["server_now"] = now
    return json.dumps(data, separators=(",", ":")).encode()


def _hit(key: str) -> Response | None:
    entry = _mc.get(key)
    if entry and time.monotonic() < entry[0]:
        _, status, headers, body = entry
        out = {**headers, "x-cache": "HIT"}
        return Response(_fresh_server_now(body), status_code=status, headers=out)
    return None


# --------------------------------------------------------------------- forward
def _upstream_headers(request: Request, peer: str) -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers["x-real-ip"] = peer
    headers["x-forwarded-for"] = peer
    headers["x-forwarded-proto"] = request.url.scheme
    return headers


async def forward(request: Request, bases: list[str], path: str, *, collect: bool = False) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"code": "VALIDATION_ERROR", "message": "request body too large"}, status_code=413)
    peer = request.client.host if request.client else "0.0.0.0"
    headers = _upstream_headers(request, peer)
    qs = ("?" + request.url.query) if request.url.query else ""
    sse = bool(_SSE.match(path))
    # Replaying after bytes were sent is only allowed for idempotent-by-contract writes.
    replay_ok = request.method == "POST" and bool(_IDEMPOTENT_POST.match(path)) and (
        not path.endswith("/claim") or "idempotency-key" in request.headers
    )
    attempts_after_send = 1 if replay_ok else 0

    start = next(_rr)
    for i in range(len(bases)):
        base = bases[(start + i) % len(bases)]
        try:
            req = _client.build_request(request.method, f"{base}/{path}{qs}", headers=headers, content=body,
                                        timeout=_sse_timeout if sse else httpx.USE_CLIENT_DEFAULT)
            resp = await _client.send(req, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            _down_until[base] = time.monotonic() + PASSIVE_DOWN_S
            continue  # nothing reached the replica: trying another is safe for every method
        except httpx.HTTPError:
            if attempts_after_send > 0:
                attempts_after_send -= 1
                continue
            return JSONResponse({"code": "INTERNAL", "message": "upstream error"}, status_code=502)
        out = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
        out["x-upstream"] = base.rsplit(":", 1)[-1]  # which replica answered (debugging, drain tests)
        if resp.headers.get("x-draining"):
            _ready[base] = False  # the replica says it is draining: no NEW requests, effective immediately
        if sse:
            out["x-accel-buffering"] = "no"
            out["cache-control"] = "no-cache"
        if collect:  # microcache path: read the whole (small) body
            data = await resp.aread()
            await resp.aclose()
            return Response(data, status_code=resp.status_code, headers=out)
        return StreamingResponse(
            resp.aiter_raw(), status_code=resp.status_code, headers=out, background=BackgroundTask(resp.aclose)
        )
    return JSONResponse({"code": "INTERNAL", "message": "no upstream available"}, status_code=503, headers={"Retry-After": "1"})


def _unavailable() -> Response:
    return JSONResponse({"code": "INTERNAL", "message": "no upstream available"}, status_code=503, headers={"Retry-After": "1"})


async def api(request: Request) -> Response:
    path = request.path_params["path"]
    if path == "metrics":
        return JSONResponse({"code": "NOT_FOUND", "message": "not found"}, status_code=404)

    peer = request.client.host if request.client else "0.0.0.0"
    if not _sim_bypass(request):
        wait = edge_allow(peer)
        if wait:
            ms = int(wait * 1000) + 1
            return JSONResponse(
                {"code": "RATE_LIMITED", "message": "too many requests", "details": {"retry_after_ms": ms, "scope": "ip"}},
                status_code=429, headers={"Retry-After": str(max(1, -(-ms // 1000)))},
            )

    bases = routable(replicas())
    if not bases:
        return _unavailable()

    if request.method == "GET" and MICROCACHE_TTL_S > 0 and _PUBLIC_CACHEABLE.match(path):
        key = f"{path}?{request.url.query}"
        hit = _hit(key)
        if hit:
            return hit
        # Single flight: 50,000 people loading the same event page cause ONE upstream request.
        async with _mc_locks.setdefault(key, asyncio.Lock()):
            hit = _hit(key)
            if hit:
                return hit
            resp = await forward(request, bases, path, collect=True)
            if resp.status_code == 200:
                headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
                _mc[key] = (time.monotonic() + MICROCACHE_TTL_S, 200, headers, bytes(resp.body))
            resp.headers["x-cache"] = "MISS"
            return resp
    return await forward(request, bases, path)


async def sim(request: Request) -> Response:
    return await forward(request, [SIMULATOR_URL], request.path_params["path"])


async def health(_: Request) -> Response:
    return PlainTextResponse("ok\n")


async def static(request: Request) -> Response:
    rel = request.path_params.get("path", "")
    target = (FRONTEND / rel).resolve()
    if FRONTEND not in target.parents and target != FRONTEND:
        return PlainTextResponse("not found", status_code=404)  # path traversal attempt
    if target.is_file():
        return FileResponse(target)
    index = FRONTEND / "index.html"
    return FileResponse(index) if index.is_file() else PlainTextResponse("no frontend", status_code=404)


# -------------------------------------------------------------------------------- ops dashboard
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
OPS_DIR = Path(__file__).resolve().parent / "ops"


async def ops_page(_: Request) -> Response:
    return FileResponse(OPS_DIR / "index.html", headers={"Cache-Control": "no-store"})


async def ops_metrics(request: Request) -> Response:
    """Sum every replica's Prometheus samples into one snapshot (counters, gauges and histogram buckets add
    up across replicas). Admin only; local development tool."""
    import hmac

    tok = request.headers.get("x-admin-token", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(tok.encode(), ADMIN_TOKEN.encode()):
        return JSONResponse({"code": "FORBIDDEN", "message": "admin token required"}, status_code=403)
    from prometheus_client.parser import text_string_to_metric_families

    merged: dict[tuple[str, tuple], float] = {}
    reps = []
    for base in replicas():
        info = {"base": base, "up": False, "ready": False, "draining": False}
        try:
            m = await _client.get(f"{base}/metrics", timeout=httpx.Timeout(1.5))
            r = await _client.get(f"{base}/readyz", timeout=httpx.Timeout(1.5))
            info.update(up=m.status_code == 200, ready=r.status_code == 200, draining=r.status_code == 503 and "draining" in r.text)
            for fam in text_string_to_metric_families(m.text):
                for smp in fam.samples:
                    if smp.name.endswith("_created"):
                        continue
                    key = (smp.name, tuple(sorted(smp.labels.items())))
                    merged[key] = merged.get(key, 0.0) + smp.value
        except httpx.HTTPError:
            pass
        reps.append(info)
    return JSONResponse({
        "ts": time.time(),
        "replicas": reps,
        "samples": [[n, dict(lbl), v] for (n, lbl), v in merged.items()],
    })


M = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
app = Starlette(
    routes=[
        Route("/gateway-health", health),
        Route("/ops/api/metrics", ops_metrics, methods=["GET"]),
        Route("/ops/", ops_page, methods=["GET"]),
        Route("/api/{path:path}", api, methods=M),
        Route("/sim/{path:path}", sim, methods=M),
        Route("/{path:path}", static, methods=["GET", "HEAD"]),
    ],
    lifespan=lifespan,
)
