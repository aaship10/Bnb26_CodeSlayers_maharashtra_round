# Rate limiting and edge (stage 3)

Two layers, deliberately different jobs.

| layer | where | job | precision |
|---|---|---|---|
| **edge** | `infra/local/gateway.py` (local) / `infra/nginx/` (reference, untested) | stop absurd floods before they reach a replica; collapse stampedes with a 1 s microcache | coarse, per IP, generous |
| **app** | Redis token buckets, `backend/app/defence/ratelimit/` | per-endpoint fairness: per identity, device, IP, /24 subnet in ONE atomic Lua call | precise, per event config |

## App layer

`@app.post("/events/{event_id}/enter", dependencies=[Depends(rate_limit("enter"))])` (also
`status`, `claim`, `challenge`). It runs **before authentication and before any database work of
the route**:

* identity = the JWT's `sub`, decoded statelessly (no lookup); no/invalid token still gets limited
  by device / IP / subnet, then the route answers 401;
* the event's config comes from a per-process cache (`config_source`, 1.5 s TTL, single-flight), so
  Postgres is not on the shed path. One lookup per event per replica per TTL;
* layer disabled or endpoint not configured → returns at once, **zero Redis calls** (tested);
* a rejected request deducts nothing and writes nothing, so a flood does not burn Redis writes and
  cannot drain the other dimensions; accepted requests deduct from all dimensions or none.

429 contract: `{code: "RATE_LIMITED", message, details: {retry_after_ms, scope}}` with a
`Retry-After` header (whole seconds). The wait is stretched by a random 0–25 % (`retry_jitter_max`)
so a crowd rejected in the same instant does not return in the same instant. `scope` is the first
dimension that denied: `identity | device | ip | subnet` (`email` for registration).

Defaults (starting points to tune with C's data, not measurements): `/enter` and `/claim` 3 burst,
0.3/s per identity; `/status` 2 burst, 0.5/s (≈ 1 per 2 s); per-IP 300–600 burst at 50–100/s, per-/24
1500–3000 burst, because a campus NAT puts thousands of real students behind one address.

### Config propagation
`PATCH /admin/events/{id}/config` takes effect on every replica within ~1.5 s (cache TTL). A stored
config that fails validation, or a database error during the lookup, keeps the **last good value**
(or the `rate_limit` preset if there is none). It never silently becomes "no defences".

### Redis failure (`fail_mode`)
`local` (the default) enforces the same buckets with a per-process fallback limiter, divided by the replica count:
protection degrades, it does not vanish. `open` lets everything through. `closed` returns `503 + Retry-After: 2` so the
database is protected at the cost of availability. A circuit breaker stops each request paying the 500 ms socket timeout
while Redis is down. Details and tests: `docs/RESILIENCE.md`. Allocation integrity never depends on Redis.

**Lesson from building this:** because the limiter fails open, a Redis it cannot use is invisible in
API responses. Two such faults were found only through logs: redis-py 8 speaks RESP3 and Redis < 6
rejects `HELLO 3` (fixed: `protocol=2`), and an exhausted connection pool raised under a burst
(fixed: a `BlockingConnectionPool` that makes callers wait up to 0.5 s). The startup script load now
logs `RATE LIMITER CANNOT USE REDIS` at error level, and stage 6 added `fd_redis_errors_total`, `fd_limiter_degraded_total`
and the circuit breaker, so this can no longer be invisible (`docs/OBSERVABILITY.md`).

## What it stops, and what it does not

| stops | does NOT stop |
|---|---|
| one identity or device hammering `/enter`, `/status`, `/claim` | an attacker with many real identities, each polite (that is the risk engine's and the allocation rule's job) |
| one IP flooding (spoofed `X-Forwarded-For` / `X-Real-IP` / `X-Sim-Client-IP` earn no extra budget: tested 900 spoofed requests → 841 limited by `ip`) | an attacker spread over many IPs *and* devices at a per-identity-compliant rate |
| a Sybil minting identities from one device (registration: 5 per device, tested) | rotating device ids (client-chosen, weak evidence) |
| a stampede on the event page (microcache: 30 concurrent requests → 1 upstream request, tested) | |

**Cost to a legitimate user:** none in normal use (a person does not enter 3 times in 10 s). Risk: a
whole campus NAT is shared, hence the generous IP/subnet buckets; if a hostel really sends >50
requests/s on `/enter` from one address, raise `ip.capacity` for that event.

## Edge details (gateway; nginx equivalents in `infra/nginx/`)

* Forwarding headers are **overwritten** with the TCP peer. Client values never reach the app, which
  also trusts them only from `TRUSTED_PROXIES`.
* Edge limiter: `EDGE_BURST=1500`, `EDGE_RPS=400` per IP (`EDGE_RPS=0` disables). Requests with a valid
  simulation key bypass it, so C's load generator is not throttled as one IP.
* Microcache: `GET /events` and `GET /events/{id}` for 1 s. The gateway patches a **fresh
  `server_now`** into every cache hit: clients derive their clock offset from it. nginx cannot do that,
  so with nginx a hit may carry a clock up to 1 s old (documented in `nginx.conf`).
* Retries: a refused connection is skipped to the next replica (safe for any method). After the request
  was sent, **only `POST …/enter` and `POST …/claim` (with `Idempotency-Key`) are replayed**, because both
  are idempotent by contract. Replaying `/auth/register` could send two emails, so it never is.
* SSE `/events/{id}/stream`: no read timeout (heartbeats are ~20 s apart), no retry, `X-Accel-Buffering: no`.
  (The stub does not implement the stream yet; the UI falls back to polling and the browser test
  tolerates that one 404.)
* `/api/metrics` is never exposed on the public origin.

## Verified how

`backend/tests_defence/test_ratelimit.py` (Lua maths, atomicity: 200 concurrent calls, exactly 25 pass,
on real Redis and fakeredis), `test_ratelimit_gate.py` (layer off vs on, dimensions, spoofing, config
cache, failure modes), `test_local_infra.py` (gateway edge behaviour), and live:
`scripts/smoke_attacks.py` (each attack with the layer off, then on).
