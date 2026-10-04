# Observability (stage 6)

What to look at while the system is under a crowd or an attack, without installing anything.

## Where to look

| what | where | needs |
|---|---|---|
| **Ops dashboard** | `http://127.0.0.1:8080/ops/` (enter `ADMIN_TOKEN` from `infra/.env`) | nothing: served by the dev gateway |
| per-replica Prometheus metrics | `http://127.0.0.1:<replica port>/metrics` (8001, 8002, …) | replicas listen on localhost only |
| decision log | `GET /admin/defence/decisions` and `/decisions/summary` (see `DEFENCES.md`) | `X-Admin-Token` |
| optional Prometheus + Grafana | `infra/prometheus/prometheus.yml`, `infra/grafana/dashboards/fairdrop.json` | your own Prometheus/Grafana binaries (**untested here**) |

`/metrics` is **not** reachable through the public origin (`/api/metrics` is 404 at the gateway and in the nginx
reference config): it is scraped from each replica directly, and the ops page aggregates it (admin token required).

## The ops dashboard

Four charts and six tiles, refreshed every 2 s, summed over all replicas: requests/s by response class, latency
p50/p95/p99 (estimated from histogram buckets), gate decisions/s (ALLOW / CHALLENGE / REJECT), protection events/s
(429s, challenges issued and solved); tiles for replicas ready and draining, the Redis circuit, requests handled
without Redis, Redis/Postgres error counts, decision-log health (written / dropped / failed), and in-flight requests
and live streams. Hover for exact values; "Show data tables" gives every chart as a table. Rates are computed from
the change between two scrapes, so the first 2 s show nothing. Light and dark themes follow the OS.
Honest limits: it keeps ~4 minutes of history in the browser tab (reload loses it); it is a development tool,
not an alerting system.

## Metrics (all `fd_*`, one private registry per replica)

Labels have bounded cardinality: routes are the **route template** (`/events/{event_id}/enter`), never a raw path
or id; scopes, reasons and bands come from fixed lists. Nothing here contains an email, user id or IP, and nothing
needs or reads Member C's ground-truth label (tested).

| metric | type | labels | answers |
|---|---|---|---|
| `fd_http_requests_total` | counter | method, route, status | how much traffic, how many errors |
| `fd_http_request_duration_seconds` | histogram | route | how slow (404 scans get no series) |
| `fd_http_requests_in_flight` | gauge | | how busy right now |
| `fd_gate_decisions_total` | counter | action, layer, weight | what the defences decided |
| `fd_risk_band_total` | counter | band | how risky the crowd looks |
| `fd_rate_limited_total` | counter | endpoint, scope | who is being throttled, by which dimension |
| `fd_challenges_issued_total` / `_solved_total` | counter | type | are people getting through the challenge |
| `fd_challenges_failed_total` | counter | type, reason | forged / replay / bad_solution / expired / wrong_binding / provider_error |
| `fd_redis_errors_total`, `fd_pg_errors_total` | counter | op | which dependency is failing and where |
| `fd_limiter_degraded_total` | counter | mode | requests handled without Redis, by policy |
| `fd_redis_breaker_open`, `fd_redis_breaker_trips_total` | gauge / counter | | is Redis being skipped |
| `fd_draining` | gauge | | is this replica draining |
| `fd_sse_streams_open`, `fd_sse_streams_closed_for_drain_total` | gauge / counter | | live streams |
| `fd_decision_log_written/dropped/failed_batches/queue` | gauge | | is analytics keeping up (read at scrape time) |

**How to read it during an event.** Rising `fd_limiter_degraded_total` or an open `fd_redis_breaker_open` means Redis
trouble: protection has degraded to the local limiter (see `RESILIENCE.md`). `fd_decision_log_dropped > 0` means the
log queue overflowed (analytics lost, entries unaffected). A rising `challenges_failed{reason="bad_solution"}` with
flat `solved` means clients cannot solve what they are given (difficulty too high for the crowd's devices?).

## Keeping the dashboards honest

`backend/tests_defence/test_observability.py` fails if the Grafana JSON or the ops page queries a metric that the
backend does not register, if the documented list and the registry differ, if a label can carry an id/email/uuid,
or if junk URLs create unbounded series. The Prometheus/Grafana files themselves have never been loaded into a real
Prometheus/Grafana here (no Docker, large downloads): the test proves they name real metrics, not that they render.
