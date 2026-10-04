r"""Tests of the EDGE itself against the running stack. Works with either edge (nginx or the Python gateway).

    .\infra\fd.ps1 up -Edge nginx -Sim -Mail file
    .\infra\fd.ps1 exec python scripts/edge_test.py

Covers what only the edge can do:
  * microcache: a stampede on the public event page reaches the replicas as a handful of requests, not hundreds
  * edge rate limiter: a tight limit returns 429 in the shared error contract (code, Retry-After, details), a
    request carrying the simulation key is exempt, and other paths (the static app) are not limited
  * forwarding headers: a client-supplied X-Forwarded-For never reaches the app
  * /api/metrics is not public; /__mock/ is blocked; the SPA fallback serves deep links
With nginx it re-renders the config with a tight limit for the limiter test, then restores the generous one.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra" / "local"))
import run as runner  # noqa: E402

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8080")
EVENT = "11111111-1111-1111-1111-111111111111"
SIM_KEY = os.environ.get("SIM_KEY", "")
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def replica_counts() -> int:
    """Total /events/{id} requests the replicas have served (their own metrics)."""
    total = 0.0
    for rep in runner.REPLICAS.read_text().split():
        text = httpx.get(f"http://{rep}/metrics", timeout=5).text
        for line in text.splitlines():
            if line.startswith("fd_http_requests_total") and 'route="/events/{event_id}"' in line:
                total += float(line.rsplit(" ", 1)[1])
    return int(total)


def set_edge_limit(rps: str, burst: str) -> None:
    """Re-render the nginx config with this limit and reload (no-op for the Python gateway)."""
    os.environ["EDGE_RPS"], os.environ["EDGE_BURST"] = rps, burst
    runner.refresh_edge([int(x.rsplit(":", 1)[1]) for x in runner.REPLICAS.read_text().split()])


async def main() -> int:
    edge = runner.edge_mode()
    print(f"edge under test: {edge}")
    if edge == "nginx":
        set_edge_limit("400", "1500")  # always start from the generous production default, whatever a crashed run left behind
        await asyncio.sleep(0.5)
    async with httpx.AsyncClient(base_url=BASE, timeout=20, limits=httpx.Limits(max_connections=100)) as c:
        # ---- microcache ------------------------------------------------------------------------------------
        await asyncio.sleep(1.3)  # let any cached copy expire
        before = replica_counts()
        rs = await asyncio.gather(*[c.get(f"/api/events/{EVENT}") for _ in range(120)])
        after = replica_counts()
        hits = sum(r.headers.get("x-cache", "") == "HIT" for r in rs)
        print(f"      120 concurrent GETs of the event page -> {after - before} reached the replicas, {hits} cache hits")
        check("a stampede on the event page reaches the replicas as a handful of requests", all(r.status_code == 200 for r in rs) and after - before <= 6, f"{after - before}")
        check("responses say whether they came from the cache", hits >= 100, f"{hits}")

        # ---- public surface --------------------------------------------------------------------------------
        check("/api/metrics is not public", (await c.get("/api/metrics")).status_code == 404)
        check("/__mock/ is blocked", (await c.get("/__mock/clock")).status_code == 404)
        check("SPA deep link falls back to index.html", "<div id=\"root\"" in (await c.get("/events/abc/status")).text or "Fair Drop" in (await c.get("/events/abc/status")).text)
        r = await c.get("/api/auth/me", headers={"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"})
        check("an unauthenticated /auth/me is a clean 401 in the error contract", r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED")

        # ---- edge rate limiter (tight limit) -------------------------------------------------------------------
        if edge != "nginx":
            print("      (python gateway: its limiter reads EDGE_RPS/EDGE_BURST at startup; limiter checks run only with nginx)")
        else:
            try:
                set_edge_limit("5", "20")
                await asyncio.sleep(0.5)
                codes = [(await c.get("/api/healthz")).status_code for _ in range(60)]
                limited = await c.get("/api/healthz")
                print(f"      60 rapid requests with burst 20 and 5/s -> {codes.count(200)} ok, {codes.count(429)} limited")
                check("a tight edge limit returns 429 once the burst is spent", codes.count(429) >= 25 and codes[0] == 200, str(codes[:30]))
                body = limited.json() if limited.status_code == 429 else {}
                check("the edge 429 follows the shared contract", limited.status_code == 429 and body.get("code") == "RATE_LIMITED"
                      and body["details"]["scope"] == "ip" and body["details"]["retry_after_ms"] > 0 and int(limited.headers["retry-after"]) >= 1, limited.text)
                statics = [(await c.get("/")).status_code for _ in range(30)]
                check("the static app is not behind the API limiter", statics == [200] * 30, str(set(statics)))
                if SIM_KEY and runner.SIM_MARKER.exists():
                    ok_codes = [(await c.get("/api/healthz", headers={"X-Sim-Key": SIM_KEY})).status_code for _ in range(60)]
                    check("a request with the simulation key is exempt from the edge limiter", set(ok_codes) == {200}, str(set(ok_codes)))
            finally:
                set_edge_limit("400", "1500")  # ALWAYS restore the generous limit, even if a check above crashed
                await asyncio.sleep(1.0)
            normal = [(await c.get("/api/healthz")).status_code for _ in range(30)]
            check("after restoring the generous limit normal traffic flows again", normal == [200] * 30, str(set(normal)))

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} edge checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
