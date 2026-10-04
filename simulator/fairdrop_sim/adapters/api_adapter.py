"""HTTP adapter for the Fair Drop API (A's routes, B's headers). The ONLY place that
knows URL shapes, header names and error codes; everything else sees `Resp`.

Two halves:
  * `AdminApi`  - lifecycle/analytics calls made by the run coordinator (not timed).
  * `UserApi`   - enter/status/claim made by simulated clients through the open-loop
                  `Sender`, which records latency from the *intended* send time.

The timed path uses aiohttp: measured on this machine, httpx's async pool collapsed
under concurrency (448 rps sequential -> 76 rps at 500 in flight) while aiohttp held
1.1-1.7k rps against the same server. httpx is kept for the low-volume admin calls.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import httpx

from fairdrop_sim.engine.recorder import Recorder

# Client-side pseudo error codes (never sent by the server).
TIMEOUT = "TIMEOUT"
CONN_ERROR = "CONN_ERROR"
BAD_RESPONSE = "BAD_RESPONSE"


@dataclass(slots=True)
class Resp:
    status: int  # 0 when no HTTP response arrived
    code: str | None  # error code from the body, or a client-side pseudo code; None on success
    body: dict[str, Any] | list[Any] | None
    headers: dict[str, str] = field(default_factory=dict)
    t_recv: float = 0.0  # perf_counter when the response (or failure) was observed

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def retry_after_s(self) -> float | None:
        if self.code != "RATE_LIMITED":
            return None
        if isinstance(self.body, dict):
            ms = (self.body.get("details") or {}).get("retry_after_ms")
            if isinstance(ms, (int, float)):
                return ms / 1000
        ra = self.headers.get("retry-after")
        return float(ra) if ra and ra.replace(".", "", 1).isdigit() else None


@dataclass(frozen=True, slots=True)
class Identity:
    """What one simulated client presents to the server. Contains no label."""

    user_id: str
    device_id: str
    client_ip: str | None = None  # sent as X-Sim-Client-IP when set
    token: str | None = None  # Bearer JWT (jwt mode)


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "dev_header"  # dev_header | jwt_sim_tokens
    sim_key: str | None = None
    send_sim_ip: bool = True


def identity_headers(ident: Identity, auth: AuthConfig) -> dict[str, str]:
    h = {"X-Device-Id": ident.device_id}
    if auth.mode == "jwt_sim_tokens":
        if not ident.token:
            raise ValueError(f"jwt mode but no token for {ident.user_id}")
        h["Authorization"] = f"Bearer {ident.token}"
    else:
        h["X-User-Id"] = ident.user_id
        if auth.sim_key:
            h["X-Sim-Key"] = auth.sim_key  # lets X-User-Id through even when the server runs AUTH_MODE=jwt
    if auth.send_sim_ip and ident.client_ip and auth.sim_key:
        h["X-Sim-Client-IP"] = ident.client_ip
        h["X-Sim-Key"] = auth.sim_key
    return h


def _parse(status: int, raw: bytes, headers: Any, t_recv: float) -> Resp:
    hdrs = {k.lower(): v for k, v in headers.items()}
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        return Resp(status, BAD_RESPONSE, None, hdrs, t_recv)
    code = None
    if status >= 400:
        code = body.get("code") if isinstance(body, dict) else None
        code = code or f"HTTP_{status}"
    return Resp(status, code, body, hdrs, t_recv)


def make_session(base_url: str, max_in_flight: int, keepalive_s: float = 15.0) -> aiohttp.ClientSession:
    """keepalive_s must stay below the server's idle timeout (nginx default 75 s), or the
    client reuses sockets the server just closed and logs spurious CONN_ERRORs."""
    connector = aiohttp.TCPConnector(limit=max_in_flight, limit_per_host=max_in_flight, ttl_dns_cache=300,
                                     keepalive_timeout=keepalive_s)
    return aiohttp.ClientSession(base_url=base_url, connector=connector, cookie_jar=aiohttp.DummyCookieJar())


# --------------------------------------------------------------------------- open-loop sender


async def sleep_until(target_perf: float) -> None:
    delay = target_perf - time.perf_counter()
    if delay > 0:
        await asyncio.sleep(delay)


class Sender:
    """Open-loop request execution.

    The caller passes the time the request *should* leave (from the schedule). We wait
    until then, then for an in-flight slot, then send. Latency is recorded from the
    intended time, so client-side queueing and a slow server both show up instead of
    silently thinning the load (no coordinated omission). Service time (from actual
    send) and scheduler lag (wake-up minus intended) are recorded separately so a
    saturated load generator is visible in the results."""

    def __init__(self, session: aiohttp.ClientSession, recorder: Recorder, max_in_flight: int, timeout_s: float):
        self.session = session
        self.rec = recorder
        self.sem = asyncio.Semaphore(max_in_flight)
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self.in_flight = 0

    async def send(self, endpoint: str, cls: str, intended: float, method: str, url: str,
                   headers: dict[str, str]) -> Resp:
        await sleep_until(intended)
        self.rec.lag(time.perf_counter() - intended)
        async with self.sem:
            self.in_flight += 1
            t_send = time.perf_counter()
            try:
                async with self.session.request(method, url, headers=headers, timeout=self.timeout) as r:
                    raw = await r.read()
                    resp = _parse(r.status, raw, r.headers, time.perf_counter())
            except TimeoutError:  # asyncio.TimeoutError and aiohttp's timeout errors
                resp = Resp(0, TIMEOUT, None, {}, time.perf_counter())
            except aiohttp.ClientError:
                resp = Resp(0, CONN_ERROR, None, {}, time.perf_counter())
            finally:
                self.in_flight -= 1
        self.rec.request(endpoint, cls, resp.status, resp.code, intended, t_send, resp.t_recv)
        return resp


class UserApi:
    """Timed client calls. `cls` is the class label for *our* bookkeeping only; it never
    reaches the wire (identity_headers takes no label)."""

    def __init__(self, sender: Sender, event_id: str, auth: AuthConfig):
        self.s = sender
        self.ev = event_id
        self.auth = auth

    def _h(self, ident: Identity, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = identity_headers(ident, self.auth)
        if extra:
            h.update(extra)
        return h

    async def enter(self, ident: Identity, cls: str, intended: float, challenge: tuple[str, str] | None = None) -> Resp:
        extra = {"X-Challenge-Id": challenge[0], "X-Challenge-Solution": challenge[1]} if challenge else None
        return await self.s.send("enter", cls, intended, "POST", f"/events/{self.ev}/enter", self._h(ident, extra))

    async def status(self, ident: Identity, cls: str, intended: float) -> Resp:
        return await self.s.send("status", cls, intended, "GET", f"/events/{self.ev}/status", self._h(ident))

    async def claim(self, ident: Identity, cls: str, intended: float, idem_key: str,
                    challenge: tuple[str, str] | None = None) -> Resp:
        extra = {"Idempotency-Key": idem_key}
        if challenge:
            extra |= {"X-Challenge-Id": challenge[0], "X-Challenge-Solution": challenge[1]}
        return await self.s.send("claim", cls, intended, "POST", f"/events/{self.ev}/claim", self._h(ident, extra))


# --------------------------------------------------------------------------- admin


class AdminError(RuntimeError):
    def __init__(self, what: str, r: httpx.Response):
        try:
            body = r.json()
        except ValueError:
            body = r.text[:300]
        super().__init__(f"{what}: HTTP {r.status_code} {body}")
        self.status = r.status_code
        self.body = body


class AdminApi:
    """Coordinator-side calls (not part of the measured load)."""

    def __init__(self, base_url: str, admin_token: str, timeout_s: float = 60):
        self.c = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, headers={"X-Admin-Token": admin_token},
                                   limits=httpx.Limits(keepalive_expiry=2.0))

    async def aclose(self) -> None:
        await self.c.aclose()

    async def _call(self, what: str, method: str, url: str, **kw: Any) -> Any:
        r = await self.c.request(method, url, **kw)
        if r.status_code >= 400:
            raise AdminError(what, r)
        return r.json() if r.content else None

    async def health(self) -> dict[str, Any]:
        r = await self.c.get("/health")
        return r.json() if r.status_code == 200 else {}

    async def create_event(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("create event", "POST", "/admin/events", json=body)

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        r = await self.c.get(f"/events/{event_id}")
        return r.json() if r.status_code == 200 else None

    async def reset(self, event_id: str, server_seed_hex: str | None) -> dict[str, Any]:
        body = {"server_seed_hex": server_seed_hex} if server_seed_hex else {}
        return await self._call("reset", "POST", f"/admin/events/{event_id}/reset", json=body)

    async def set_defences(self, event_id: str, defences: dict[str, Any]) -> dict[str, Any]:
        return await self._call("config", "PATCH", f"/admin/events/{event_id}/config", json={"defences": defences})

    async def schedule(self, event_id: str, opens_at_iso: str, window_seconds: float) -> dict[str, Any]:
        return await self._call("schedule", "POST", f"/admin/events/{event_id}/schedule",
                                json={"opens_at": opens_at_iso, "window_seconds": window_seconds})

    async def close(self, event_id: str) -> dict[str, Any]:
        return await self._call("close", "POST", f"/admin/events/{event_id}/close")

    async def draw(self, event_id: str) -> dict[str, Any]:
        return await self._call("draw", "POST", f"/admin/events/{event_id}/draw", json={})

    async def stats(self, event_id: str) -> dict[str, Any]:
        return await self._call("stats", "GET", f"/admin/events/{event_id}/stats")

    async def invariants(self, event_id: str) -> dict[str, Any]:
        return await self._call("invariants", "GET", f"/admin/events/{event_id}/invariants")

    async def sim_tokens(self, user_ids: list[str]) -> dict[str, str]:
        out = await self._call("sim tokens", "POST", "/admin/sim/tokens", json={"user_ids": user_ids})
        return out["tokens"]

    # mock-only analytics (the real target uses db_adapter + A's verifier CLI)
    async def mock_export(self, event_id: str) -> dict[str, Any]:
        return await self._call("export", "GET", f"/__mock/events/{event_id}/export")

    async def mock_verify(self, event_id: str) -> dict[str, Any]:
        return await self._call("verify", "GET", f"/__mock/events/{event_id}/verify")
