# Resilience (stage 6)

What happens when parts of the system fail or are restarted, what was measured, and what is still open.
Chaos scripts that inject faults into a loaded system are stage 7; this stage covers the mechanisms and their tests.

## Failure policy (current behaviour)

| failure | behaviour |
|---|---|
| **Redis down**, `fail_mode: local` (default) | rate limits are enforced by a per-process fallback limiter (same buckets, divided by the replica count). Protection degrades, it does not vanish. Load = 0, timing signal empty, single-use tracking skipped, cluster counts come from Postgres |
| Redis down, `fail_mode: open` | no rate limiting at all |
| Redis down, `fail_mode: closed` | rate-limited endpoints answer `503 Retry-After: 2` (protects the database, sacrifices availability) |
| Redis slow or blackholed | the **circuit breaker** opens after 5 consecutive connection failures: for 3 s Redis calls fail instantly instead of costing a 500 ms timeout each, then one probe call decides |
| Postgres down | new entries cannot proceed (A's allocation needs it anyway); the rate limiter keeps working; the decision log drops batches (counted); config lookups serve the last good value |
| risk/signal computation raises | the entry proceeds without a score, logged at ERROR, counted in `fd_pg_errors_total{op="risk_assessment"}` |
| CAPTCHA provider down | retryable `503 Retry-After: 3` (never a lockout, never a free pass) |
| stored config invalid | the last good config is kept; never "no defences" |
| a replica dies | the gateway skips it on the first refused connection and probes `/readyz` every 0.25 s; in-flight requests on it are lost (clients retry; `/enter` and `/claim` are replay-safe by contract) |
| a replica is drained | see below: zero failed requests (measured) |

Allocation integrity never depends on Redis in any mode: the database enforces it.

### What "local" does NOT give you
The fallback limiter is per process. With N replicas each enforces its own copy, so the cluster-wide limit during an outage is
only approximately the configured one (capacities are divided by `FD_REPLICA_COUNT`, which assumes traffic is spread evenly), and a
user hitting different replicas gets a separate budget on each. It is deliberately weaker than Redis and says so in its metrics
(`fd_limiter_degraded_total{mode="local"}`).

## Circuit breaker details
`GuardedRedis` wraps every command and every pipeline. Only **connection-level** failures count (refused, timeout, reset);
a reply such as `NOSCRIPT` proves Redis is alive and resets the streak. States: closed, open (3 s cool-down, all calls fail
in microseconds with `redis circuit open`), half-open (exactly one probe at a time). Tunables: `REDIS_BREAKER_THRESHOLD` (5),
`REDIS_BREAKER_COOLDOWN_S` (3). Tested with a fake clock, with mocked clients, and against a blackholed address.

## Graceful drain (rolling restarts, SIGTERM)

```
begin_drain()            /readyz -> 503 and every response carries X-Draining: 1
                         the gateway stops sending NEW requests at once (header) or within 0.25 s (readiness poll)
                         open SSE streams get `event: reconnect` + `retry: 1000` and end
... DRAIN_GRACE_S (3 s)  the replica keeps serving anything that still arrives
stop accepting           uvicorn waits up to 25 s for in-flight requests, then exits
```
Triggers: `SIGTERM` / `SIGINT` / Ctrl-Break (the launcher `infra/local/serve.py` maps them to the drain; a second signal stops
immediately) or `POST /admin/defence/lifecycle/drain {"exit_after_s": 3}` (admin token; the same code path, for rolling restarts and demos).
Liveness (`/healthz`) stays 200 while draining: do not restart a replica that is draining on purpose.

**Measured** (`scripts/drain_test.py`, real processes, 3 replicas, traffic flowing the whole time): readiness flipped after ~15 ms;
the open SSE stream received the reconnect hint and a reconnect landed on a different replica; 3 requests of 6 s already running on
the draining replica **all completed** (it had stopped accepting while they ran); the replica then exited by itself;
**132 gateway requests during the episode, 0 failed**, none routed to the drained replica after it stopped accepting. 10/10 checks.
Found and fixed by that test: with only a 1 s readiness poll, an immediate reconnect could still land on the draining replica;
fixed with the `X-Draining` header and a 0.25 s poll.
Also tested: the same drain **through nginx** (10/10 on 6 consecutive runs after the fix; nginx cannot see readiness: a draining replica refuses NEW streams with 503 and nginx retries them elsewhere, see `EDGE.md` and R32) and abrupt faults (`docs/CHAOS.md`).
Not covered: Linux signal delivery
(the handler is unit-tested; the live test uses the admin endpoint because a detached Windows process cannot receive Ctrl-Break).

## Rebuilding from Postgres
Everything Redis holds is rebuildable: cluster counts (device/IP/subnet/ASN/velocity) are recomputed from `defence.identities`
(tested: flush Redis, same numbers; a thundering herd causes one query, not one per request); rate-limit buckets simply restart
full; challenge single-use markers and timing lists expire on their own.

## Connection budget (no PgBouncer)
Per replica: the stub API pool (`DB_POOL_SIZE`, 10) + the defence package's own pool (`DEFENCE_POOL_SIZE`, 4) = 14 connections,
plus a few for migrations and tools. The private Postgres runs `max_connections=100`, so about **6 replicas** fit before the budget
is gone; raise both together beyond that. **PgBouncer was not added**: it is not installed here, the load tests showed Postgres
nearly idle (0.2 CPU-seconds per 5 s), and A's real pool/driver settings are unknown (R12). If added later, check the driver's
prepared-statement setting first (transaction pooling and server-side prepared statements do not mix).

## Scale tests (`scripts/scale_test.py`, `fd.ps1 resilience`)
A flash crowd (event page, enter, poll status twice) through real processes, then consistency checks. **All runs: 0 transport errors,
0 5xx, entries == users told "entered", no duplicates, one ALLOW decision per new entry, nothing dropped, invariant checker passes.**

| run (all on one 8-core laptop) | page rps | enter rps | status rps | load spread over replicas |
|---|---|---|---|---|
| 3 replicas, via the Python gateway, 1000 users, 1 generator | 170 | 118 | 135 | 1116 / 1117 / 1116 requests |
| 3 replicas, direct, 3000 users, 4 generators | 286 | 250 | 294 | 4164 / 4163 / 4163 |
| 1 replica, direct, same load | 232 | 232 | 230 | 12173 |
| 3 replicas, direct, 600 users, PoW + rate limits on | 320 | 86 (incl. challenge round) | 205 | 600 challenges issued = 600 solved = 600 entries |

**How to read these (they are easy to misread).** Throughput barely rose from 1 to 3 replicas (≈230 → ≈290 rps), but that is the
load generators, not the stack: a CPU sample during the 3-replica run showed the machine at 94-99 %, each of the four Python
generators using ~85 % of a core, each replica only ~48 % of a core and Postgres almost idle. One replica on its own saturated at
≈230 rps for these real endpoints (≈450 rps for a trivial `/healthz`) on Python and Windows' select-based event loop, so a replica's
ceiling is a few hundred requests per second here; the 3-replica figure is capped by the test tool, not by the replicas. Through the gateway the single-process Python gateway adds its own ceiling
(≈130-170 rps with one generator). These numbers say the stack is **correct and evenly balanced under a crowd** and let you compare
configurations; they are not a capacity promise for any real deployment (nginx, Linux, uvloop and separate machines change everything).
No test here reached 50,000 users: the largest was 3,000.

## Still open
* Abrupt faults (kill a replica / Redis / Postgres under load) and their recovery times: stage 7.
* The Prometheus/Grafana files are unverified against real servers.
* A's real `/readyz`, SSE stream and `install(app)` must honour the drain (R8, R29).
