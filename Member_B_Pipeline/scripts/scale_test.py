r"""Flash-crowd scale test against the RUNNING stack, with consistency checks afterwards.

    .\infra\fd.ps1 up -Sim -Mail file                    # simulation mode: tokens + edge bypass for this load tool
    .\infra\fd.ps1 exec python scripts/scale_test.py --users 3000 --procs 4
    .\infra\fd.ps1 exec python scripts/scale_test.py --users 3000 --procs 4 --direct     # bypass the gateway
    .\infra\fd.ps1 exec python scripts/scale_test.py --users 400 --preset rate_limit+pow --label pow

Each virtual user: loads the event page, enters the draw (solving a PoW / CAPTCHA if the preset asks), then polls
status twice. Reports client-side throughput and latency per phase, how load spread over the replicas, the
SERVER-side counters (every replica's /metrics), and verifies afterwards that
  * every user told "entered" has exactly one entry and nobody has two,
  * the decision log holds one ALLOW per new entry (nothing dropped under load),
  * A's invariant checker (the stub's) passes.

Reading the numbers (the honest part):
  * Everything shares one laptop: this tool, the gateway, N uvicorn replicas, Postgres and Redis.
  * One Python load-generator process cannot saturate even one replica, so use --procs (workers split the users).
  * The single-process Python gateway has a ceiling of its own; --direct sends each user straight to a replica
    (round robin by user) to measure the API tier without it. nginx would not have that ceiling.
  * Use the numbers to COMPARE runs (1 vs 3 replicas, preset on vs off), not to promise capacity.
Cleans up the users, entries and decisions it created unless --keep.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8080")
ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "")}
SIM_KEY = os.environ.get("SIM_KEY", "")
EVENT = "11111111-1111-1111-1111-111111111111"
NS = uuid.UUID("5ca1e5ca-1e00-4000-8000-000000000001")
PHASES = ("1 event page", "2 enter the draw", "3 poll status x2")


def solve(prefix: str, bits: int) -> str:  # top level so a process pool can run it
    base = hashlib.sha256(f"{prefix}:".encode())
    n = 0
    while True:
        h = base.copy()
        h.update(str(n).encode())
        if 256 - int.from_bytes(h.digest(), "big").bit_length() >= bits:
            return str(n)
        n += 1


def sim_captcha(uid: str) -> str:
    u, e, ts = uuid.UUID(uid).hex, uuid.UUID(EVENT).hex, int(time.time())
    mac = hmac.new(SIM_KEY.encode(), f"sim1|{u}|{e}|{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"sim1.{u}.{e}.{ts}.{mac}"


def replicas() -> list[str]:
    f = ROOT / ".local" / "run" / "replicas"
    return [ln.strip() for ln in f.read_text().splitlines() if ln.strip()]


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


# ------------------------------------------------------------------------------------- one worker process
async def shard_async(cfg: dict, idxs: list[int]) -> dict:
    """Runs all three phases for the users in `idxs`. Returns raw per-phase measurements."""
    uids, tokens, reps, direct = cfg["uids"], cfg["tokens"], cfg["replicas"], cfg["direct"]
    limits = httpx.Limits(max_connections=cfg["concurrency"], max_keepalive_connections=cfg["concurrency"])
    pool = ProcessPoolExecutor(max_workers=2) if cfg["needs_solver"] else None
    sem = asyncio.Semaphore(cfg["concurrency"])
    out = {p: {"lat": [], "status": Counter(), "upstream": Counter(), "t0": None, "t1": None} for p in PHASES}
    entered: list[int] = []

    def hdr(i: int) -> dict:
        return {"Authorization": f"Bearer {tokens[uids[i]]}", "X-Device-Id": str(uuid.uuid5(NS, f"dev-{i}")), "X-Sim-Key": SIM_KEY,
                "X-Sim-Client-IP": f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}"}

    def target(i: int, path: str) -> tuple[str, str]:
        if direct:
            rep = reps[i % len(reps)]
            return f"http://{rep}{path}", rep.rsplit(":", 1)[1]
        return f"{BASE}/api{path}", ""

    async with httpx.AsyncClient(timeout=60, limits=limits) as c:
        async def timed(phase: str, method: str, i: int, path: str, **kw):
            url, rep = target(i, path)
            ph = out[phase]
            async with sem:
                ph["t0"] = ph["t0"] or time.time()
                t = time.perf_counter()
                try:
                    resp = await c.request(method, url, **kw)
                except httpx.HTTPError as exc:
                    ph["lat"].append((time.perf_counter() - t) * 1000)
                    ph["status"][type(exc).__name__] += 1
                    ph["t1"] = time.time()
                    return None
                ph["lat"].append((time.perf_counter() - t) * 1000)
                ph["status"][resp.status_code] += 1
                ph["upstream"][rep or resp.headers.get("x-upstream", "?")] += 1
                ph["t1"] = time.time()
                return resp

        async def page(i):
            await timed(PHASES[0], "GET", i, f"/events/{EVENT}")

        async def enter(i):
            h = hdr(i)
            resp = await timed(PHASES[1], "POST", i, f"/events/{EVENT}/enter", headers=h)
            for _ in range(4):  # follow the challenge protocol like the frontend does
                if resp is None or resp.status_code != 403 or resp.json().get("code") != "CHALLENGE_REQUIRED":
                    break
                ch = resp.json()["details"]["challenge"]
                if ch["type"] == "pow":
                    sol = await asyncio.get_running_loop().run_in_executor(pool, solve, ch["pow"]["prefix"], ch["pow"]["difficulty_bits"])
                else:
                    sol = sim_captcha(uids[i])
                resp = await timed(PHASES[1], "POST", i, f"/events/{EVENT}/enter", headers={**h, "X-Challenge-Id": ch["id"], "X-Challenge-Solution": sol})
            if resp is not None and resp.status_code in (200, 201):
                entered.append(i)

        async def status(i):
            for _ in range(2):
                await timed(PHASES[2], "GET", i, f"/events/{EVENT}/status", headers=hdr(i))

        for fn in (page, enter, status):  # phases in order; users within a phase run concurrently
            await asyncio.gather(*[fn(i) for i in idxs])
    if pool:
        pool.shutdown()
    for ph in out.values():
        ph["status"], ph["upstream"] = dict(ph["status"]), dict(ph["upstream"])
    return {"phases": out, "entered": entered}


def shard_main(cfg: dict, idxs: list[int]) -> dict:
    return asyncio.run(shard_async(cfg, idxs))


# ------------------------------------------------------------------------------------------- the parent
async def scrape(c: httpx.AsyncClient) -> dict[str, dict[tuple, float]]:
    from prometheus_client.parser import text_string_to_metric_families

    out = {}
    for rep in replicas():
        samples: dict[tuple, float] = {}
        for fam in text_string_to_metric_families((await c.get(f"http://{rep}/metrics")).text):
            for s in fam.samples:
                samples[(s.name, tuple(sorted(s.labels.items())))] = s.value
        out[rep.rsplit(":", 1)[1]] = samples
    return out


def total(samples: dict, name: str, **want) -> float:
    return sum(v for (n, lbl), v in samples.items() if n == name and all(dict(lbl).get(k) == w for k, w in want.items()))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=3000)
    ap.add_argument("--concurrency", type=int, default=100, help="per worker process")
    ap.add_argument("--procs", type=int, default=1, help="load-generator processes (users are split between them)")
    ap.add_argument("--direct", action="store_true", help="bypass the gateway: each user goes straight to a replica")
    ap.add_argument("--preset", default="none")
    ap.add_argument("--label", default="")
    ap.add_argument("--json")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    if not SIM_KEY or not ADMIN["X-Admin-Token"]:
        raise SystemExit("run via: .\\infra\\fd.ps1 exec python scripts/scale_test.py  (needs ADMIN_TOKEN, SIM_KEY, DATABASE_URL)")

    uids = [str(uuid.uuid5(NS, f"scale-{i}")) for i in range(a.users)]
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        db.execute("DELETE FROM entries WHERE user_id = ANY(%s::uuid[])", (uids,))
        with db.cursor() as cur:
            cur.executemany("INSERT INTO public.users (id, email, display_name) VALUES (%s, %s, 'Scale') ON CONFLICT (id) DO NOTHING",
                            [(u, f"scale-{i}@example-college.edu") for i, u in enumerate(uids)])
        db.commit()

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        (await c.patch(f"/api/admin/events/{EVENT}/config", json={"defences": {"preset": a.preset}}, headers=ADMIN)).raise_for_status()
        await asyncio.sleep(2.0)  # replica config cache
        tokens: dict[str, str] = {}
        for i in range(0, len(uids), 2000):
            tr = await c.post("/api/admin/sim/tokens", json={"user_ids": uids[i:i + 2000], "ttl_s": 1800}, headers=ADMIN)
            if tr.status_code != 200:
                raise SystemExit("token minting failed: start the stack with  .\\infra\\fd.ps1 up -Sim -Mail file")
            tokens.update(tr.json()["tokens"])
        before = await scrape(c)
        sum0 = (await c.get(f"/api/admin/defence/decisions/summary?event_id={EVENT}", headers=ADMIN)).json()

        cfg = {"uids": uids, "tokens": tokens, "replicas": replicas(), "direct": a.direct, "concurrency": a.concurrency,
               "needs_solver": "pow" in a.preset or a.preset == "all"}
        shards = [list(range(k, a.users, a.procs)) for k in range(a.procs)]
        print(f"\n{a.users} users, {a.procs} generator process(es) x concurrency {a.concurrency}, preset {a.preset!r}, "
              f"replicas {len(replicas())}, {'DIRECT to replicas' if a.direct else 'via the gateway'}" + (f", label {a.label}" if a.label else ""))
        t_all = time.time()
        with ProcessPoolExecutor(max_workers=a.procs) as ex:
            results = list(await asyncio.gather(*[asyncio.get_running_loop().run_in_executor(ex, shard_main, cfg, s) for s in shards]))
        wall_all = time.time() - t_all

        await asyncio.sleep(1.5)  # decision-log batches (<= 0.5 s) on every replica
        after = await scrape(c)
        sum1 = (await c.get(f"/api/admin/defence/decisions/summary?event_id={EVENT}", headers=ADMIN)).json()
        inv = (await c.get(f"/api/admin/events/{EVENT}/invariants", headers=ADMIN)).json()
        await c.patch(f"/api/admin/events/{EVENT}/config", json={"defences": {"preset": "none"}}, headers=ADMIN)

    # ---- merge the workers ----------------------------------------------------------------------------------
    phases, entered_ok = [], set()
    for r in results:
        entered_ok |= set(r["entered"])
    for name in PHASES:
        lat = [x for r in results for x in r["phases"][name]["lat"]]
        status: Counter = Counter()
        up: Counter = Counter()
        for r in results:
            status.update(r["phases"][name]["status"])
            up.update(r["phases"][name]["upstream"])
        t0 = min(r["phases"][name]["t0"] for r in results if r["phases"][name]["t0"])
        t1 = max(r["phases"][name]["t1"] for r in results if r["phases"][name]["t1"])
        wall = max(0.001, t1 - t0)
        p = {"phase": name, "requests": len(lat), "wall_s": round(wall, 2), "rps": round(len(lat) / wall, 1), "p50_ms": round(pct(lat, .5), 1),
             "p95_ms": round(pct(lat, .95), 1), "p99_ms": round(pct(lat, .99), 1), "max_ms": round(max(lat, default=0), 1),
             "status": dict(status), "by_replica": dict(up)}
        phases.append(p)
        print(f"  {name:<18} {p['requests']:>6} req  {p['rps']:>7} rps   p50 {p['p50_ms']:>7} ms  p95 {p['p95_ms']:>7} ms  p99 {p['p99_ms']:>7} ms   {p['status']}")
    print(f"  (whole run {wall_all:.1f} s)")

    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        n_entries = db.execute("SELECT count(*) FROM entries WHERE user_id = ANY(%s::uuid[])", (uids,)).fetchone()[0]
        n_distinct = db.execute("SELECT count(DISTINCT user_id) FROM entries WHERE user_id = ANY(%s::uuid[])", (uids,)).fetchone()[0]
        bad_w = db.execute("SELECT count(*) FROM entries WHERE user_id = ANY(%s::uuid[]) AND weight NOT IN (1.00, 0.50, 0.25)", (uids,)).fetchone()[0]
        if not a.keep:
            db.execute("DELETE FROM entries WHERE user_id = ANY(%s::uuid[])", (uids,))
            db.execute("DELETE FROM defence.decisions WHERE user_id = ANY(%s::uuid[])", (uids,))
            db.execute("DELETE FROM public.users WHERE id = ANY(%s::uuid[])", (uids,))
        db.commit()

    # ---- server-side view -----------------------------------------------------------------------------------
    def delta(name, **w):
        return sum(total(after[p], name, **w) - total(before[p], name, **w) for p in after)

    statuses = {dict(l).get("status") for p in after for (n, l) in after[p] if n == "fd_http_requests_total"}
    server = {
        "requests_handled": delta("fd_http_requests_total"),
        "responses_5xx": sum(delta("fd_http_requests_total", status=s) for s in statuses if s and s.startswith("5")),
        "per_replica_requests": {p: round(total(after[p], "fd_http_requests_total") - total(before[p], "fd_http_requests_total")) for p in after},
        "gate_allow": delta("fd_gate_decisions_total", action="ALLOW"), "gate_challenge": delta("fd_gate_decisions_total", action="CHALLENGE"),
        "gate_reject": delta("fd_gate_decisions_total", action="REJECT"), "rate_limited": delta("fd_rate_limited_total"),
        "challenges_issued": delta("fd_challenges_issued_total"), "challenges_solved": delta("fd_challenges_solved_total"),
        "redis_errors": delta("fd_redis_errors_total"), "pg_errors": delta("fd_pg_errors_total"), "degraded": delta("fd_limiter_degraded_total"),
        "decision_log_dropped": sum(total(after[p], "fd_decision_log_dropped") for p in after),
        "decision_log_failed_batches": sum(total(after[p], "fd_decision_log_failed_batches") for p in after),
        "breaker_trips": delta("fd_redis_breaker_trips_total"),
    }
    print("\nserver-side (all replicas, this run):")
    for k, v in server.items():
        print(f"  {k:<28} {v}")

    allow = lambda s: sum(x["n"] for x in s["counts"] if x["action"] == "ALLOW")  # noqa: E731
    new_allows = allow(sum1) - allow(sum0)
    checks = [
        ("every request got an HTTP answer (no transport errors)", not any(isinstance(k, str) for p in phases for k in p["status"])),
        ("no 5xx anywhere (client view)", not any(isinstance(k, int) and k >= 500 for p in phases for k in p["status"])),
        ("no 5xx anywhere (server view)", server["responses_5xx"] == 0),
        ("entries == users who were told 'entered'", n_entries == len(entered_ok)),
        ("nobody has two entries", n_entries == n_distinct),
        ("every weight is 1.0 / 0.5 / 0.25", bad_w == 0),
        ("decision log: one ALLOW per new entry, nothing lost under load", new_allows == n_entries and server["decision_log_dropped"] == 0
         and server["decision_log_failed_batches"] == 0),
        ("the invariant checker passes", inv.get("passed") is True),
    ]
    print("\nconsistency after the crowd:")
    failed = 0
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed += not ok
    print(f"  ({n_entries} entries for {a.users} users; {new_allows} new ALLOW decisions logged)")
    if a.json:
        Path(a.json).write_text(json.dumps({"users": a.users, "procs": a.procs, "concurrency": a.concurrency, "direct": a.direct, "preset": a.preset,
                                            "label": a.label, "replicas": len(replicas()), "phases": phases, "server": server,
                                            "entries": n_entries, "wall_s": round(wall_all, 1)}, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
