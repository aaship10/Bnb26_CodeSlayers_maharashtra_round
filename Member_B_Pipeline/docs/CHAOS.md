# Chaos tests

`infra/chaos/chaos.py` runs a short load through the **public edge**, injects ONE fault, heals it, and checks the
invariants. Every fault is a hard process kill or suspend: nothing gets to clean up.

```powershell
.\infra\fd.ps1 up -Edge nginx -Sim -Mail file     # simulation mode + file mail: it creates made-up users, they must never get real email
.\infra\fd.ps1 chaos all                          # or one of: kill-replica kill-worker restart-redis pause-postgres terminate-pg-connections restart-edge
.\infra\fd.ps1 up                                 # back to normal mode afterwards
```

Results: `infra/chaos/results/<fault>.json` = `{fault, recovery_seconds, requests_failed, invariants_passed, ...}` and `summary.json`.
Works with either edge (`-Edge gateway` is the default). The numbers below are from nginx.

## What the load is
16 concurrent virtual users, each takes a fresh identity (6,000 minted for the run), `POST /enter`, then polls `/status` and the
event page, repeat. 1,300 to 2,700 requests per run. The preset is `none`: this tests the plumbing, not the defences.

* `requests_failed` = transport errors + 5xx, seen by the load generator over the **whole** run (429 and 4xx are answers, not failures).
* `outage_seconds` = first failed request to last failed request.
* `recovery_seconds` = from the heal command returning to the first successful **write** (an enter by a fresh user).
* `invariants_passed` = A's checker (`/admin/events/{id}/invariants`) **and** our own checks against Postgres:
  nobody has two entries; **every user who was told "entered" has an entry** (no acknowledged write lost); entries nobody was told
  about are only requests that ended in an error (an `/enter` that died mid-flight may or may not have been recorded; that is
  allowed, a silent duplicate or a lost acknowledgement is not); every weight is 1.0 / 0.5 / 0.25; a write works again after healing.

## Results (nginx edge, 3 replicas, one machine)

| Fault | What happens | Failed requests | Outage | Recovery | Invariants |
|---|---|---|---|---|---|
| `kill-replica` (hard-kill 1 of 3, 6 s, restart) | nginx's connect to the dead replica is refused and the request is retried on another one; the replica is skipped for `fail_timeout` | **0** | 0 s | 0.08 s | PASS |
| `kill-worker` (6 s) | the stub worker only idles; `/enter` never depends on it | **0** | 0 s | 0.10 s | PASS |
| `restart-redis` (8 s down) | by design the limiter breaker opens and `fail_mode = local` takes over (proven in the stage-6 breaker tests; this run did not read the breaker counters, it shows only that no client request failed) | **0** | 0 s | 0.09 s | PASS |
| `pause-postgres` (all 30 Postgres processes suspended 5 s) | requests that need Postgres **wait** (pool timeout 5 s, client 20 s) and finish when it resumes; throughput drops by about half during the pause | **0** | 0 s | 0.10 s | PASS |
| `terminate-pg-connections` (`pg_terminate_backend` on every app connection, mid-load) | the pool replaces a dead socket before handing it out (`check=check_connection`) | **0** | 0 s | 0.12 s | PASS |
| `restart-edge` (hard-kill nginx 4 s) | there is no one to answer: connections are refused until it is back | **33** (all while nginx was down) | 4.1 s | 0.08 s | PASS |

The edge is a single point of failure on one machine. That row is the honest answer, not a bug: in production run two edges behind a
load balancer or DNS failover. No state was lost in any run.

### What the chaos run found (and fixed)
1. **`terminate-pg-connections`: 238 failed requests, 2.7 s outage** on the first run. Neither pool validated a connection before use,
   so each killed socket became a failed request. Fix: `check=AsyncConnectionPool.check_connection` on B's pool (`runtime.py`) and on
   the stub's. Re-run, twice: 0 failed. It costs one cheap `SELECT 1` round trip per checkout. **A should do the same on A's pool** (R31).
2. **The stub worker crashed on startup on Windows** (`add_signal_handler` is not implemented on Windows event loops). Nothing noticed
   because nothing depended on it; `heal` restarting it and it dying again made it visible. Fixed (stub only; A's real worker must not
   assume POSIX signals either).
3. The harness itself counted its own recovery probe as an "unexplained entry". Fixed; the probe now registers what it wrote.

## What these tests do NOT show
* **Fault durations are short** (4 to 8 s). A Postgres pause longer than the client timeout (20 s here, a browser's own limit in
  practice) will produce errors; Redis down for minutes means the local fallback limiter is the only protection (see `RESILIENCE.md`).
* **`kill-replica`**: 0 failures in 4 runs, but requests that were *in flight on the killed replica at that instant* can fail. With
  16 users that window was empty each time; under a flash crowd expect a handful. nginx replays `/enter` and `/claim` (idempotent by
  contract) on 502/503 or timeout, nothing else.
* One fault at a time. Two at once (Redis down while a replica dies) is not tested.
* A's real worker, claim holds expiring, the lottery draw and the waitlist are **not** exercised: they live in A's backend. The
  stub's invariant checker only knows "one entry per user" and "weights in the allowed set" (the stub has no seats). When A's checker
  is wired in (R10) the same harness runs the real invariants (oversold, duplicate seats, orphaned holds) with no change.
* Single machine: no network partitions, no slow disks, no clock jumps, no Linux.

## Re-running the demo's "kill a replica" step
D's `DEMO_SCRIPT.md` says `docker compose kill <api-replica>`. Without Docker: `.\infra\fd.ps1 kill 8002` (hard-kills that replica),
and `.\infra\fd.ps1 heal` brings it back. Not tested live with a browser: a hard kill ends that replica's SSE streams abruptly (unlike a drain, there is no `reconnect`
hint), so the status page should show "Reconnecting…" and resume on another replica via its own retry. The drain path (graceful) is
tested in a real browser; rehearse the hard-kill path once before the demo.
