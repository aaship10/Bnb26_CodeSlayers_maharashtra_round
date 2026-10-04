# The edge (nginx or the Python gateway)

One origin, `http://127.0.0.1:8080`: `/api/*` to the API replicas (prefix stripped), `/sim/*` to Member C's simulator, `/ops/` to the ops
dashboard, everything else the built frontend with SPA fallback. Two interchangeable implementations:

| | Python gateway (default) | nginx 1.28 (`-Edge nginx`) |
|---|---|---|
| Start | `.\infra\fd.ps1 up` | `.\infra\fd.ps1 get-nginx` once, then `.\infra\fd.ps1 up -Edge nginx` |
| Health | polls `/readyz` every 0.25 s, honours `X-Draining` | **passive only** (open-source nginx has no active checks): `max_fails`/`fail_timeout` |
| Microcache | 1 s, refreshes `server_now` inside the cached body | 1 s, **cannot** refresh `server_now` (up to 1 s stale on a HIT) |
| Rate limit | `EDGE_RPS` / `EDGE_BURST` read at startup | `limit_req`, re-rendered by `refresh_edge` |
| Use it for | day-to-day development, `/ops/` dashboard | what production looks like; the demo |

nginx is the reference for the real deployment. `/ops/` is still served by the Python gateway (it runs next to nginx on :8090 and nginx
proxies `/ops/` to it) because the dashboard aggregates all replicas' metrics.

## Files
`infra/nginx/` holds **templates**: `nginx.conf` (limiter zones, cache path, upstreams), `fairdrop.conf` (the server block),
`fd-proxy.conf`, `fd-security-headers.conf` (Member D's, unchanged). `infra/local/run.py` renders them with the replica list, paths and
(only in simulation mode) the simulation key into `.local/nginx/conf/fd/`, runs `nginx -t`, then starts or reloads. `get_nginx.py`
downloads the official nginx.org Windows build (no installer, nothing system-wide). Member D's `frontend/deploy/nginx/` is the origin
of the SPA/SSE/caching/header parts; **it is merged here, not replaced** (answers B11 and B12 in `INTERFACE_REQUESTS_B.md`).

## What the edge does
* **SPA fallback, hashed assets cached for a year, `index.html` never.** (D's rules.)
* **SSE**: no buffering, 1 h read timeout, never retried (`proxy_next_upstream off`), `X-Accel-Buffering: no`.
* **Microcache**: `GET /events` and `/events/{id}`, 1 s, one upstream request per key (`proxy_cache_lock`), stale while updating or on error.
  Measured: 120 simultaneous requests for the event page, **1 reached a replica**, 119 were cache hits.
* **Rate limit** per client IP, deliberately generous (default 400 req/s, burst 1500): a campus NAT puts thousands of students behind one
  address. The precise limits are the app's (Redis). The edge answers 429 in the shared contract (`code`, `Retry-After`,
  `details.{retry_after_ms, scope}`). Static files are not limited. A request with the **simulation key** is exempt, so Member C's load
  generator is not throttled as "one IP"; the key is only written into the config when simulation mode is on.
* **Forwarding headers are overwritten**, not appended: `X-Forwarded-For` and `X-Real-IP` are set to the real peer, so a client-supplied
  value never reaches the app (tested). Behind a CDN or load balancer use `ngx_http_realip_module` instead and trust only its addresses.
* **Retries**: a refused connection is retried on another replica (nothing was sent). `POST /enter` and `/claim` are the only writes
  nginx may replay after a 502/503/timeout (`non_idempotent`), because both are idempotent by contract (one entry per identity;
  `/claim` requires an `Idempotency-Key`). Everything else, e.g. `/auth/register`, is never replayed once sent.
* **Hidden**: `/api/metrics` (404 on the public origin; Prometheus scrapes replicas directly), `/__mock/` (404).
* **Its own failures are JSON**: when every replica is down, or the simulator is not running, or a connect/read times out, nginx answers
  `503 {"code":"INTERNAL","details":{"reason":"upstream_unavailable","retry_after_ms":2000}}` with `Retry-After`, not an HTML page.
  An app's own error is passed through untouched. Connect timeout is 2 s so a dead host does not hold a request for a minute.

## Tested (live, through nginx)
`scripts/edge_test.py` 10/10 (microcache, tight limit returns 429 in the contract, static not limited, simulation-key exemption,
`/api/metrics` and `/__mock/` hidden, SPA deep link, generous limit restored), `scripts/drain_test.py` 10/10 through nginx, repeated 6 times after the fix (0 failed requests during a drain, ~115 per run), the full `check` suite (API e2e 15/15, D's zod contract 20/20, real Chrome 24/24, smoke attacks),
`chaos.py` (see `CHAOS.md`), and D's `npm run smoke` (below). `nginx -t` is run on every start; a test renders the templates and runs it.

D's `frontend/tools/smoke.ts` against this stack: **9 of 15 pass.** All six failures are routes that belong to others and do not exist
yet: `/fairness`, `/audit`, `/audit/verify`, `/admin/.../stats` (A), `/sim/scenarios`, `/sim/experiments` (C; the edge answers its JSON
503). The previously failing invariants check now passes: the stub returns D's `invariantsSchema` shape (`oversold`,
`duplicate_users`, `duplicate_seats`, `orphaned_holds`, `passed`, `checked_at`). The authenticated attendee checks were skipped (no
`--token`); the equivalent flows are covered by `check`.

## Findings (all found by running it, all fixed)
* The simulation key (48 characters) does not fit nginx's default `map_hash_bucket_size`: `nginx -t` failed. Set to 128.
* **Drain through nginx needed a change.** The Python gateway stops routing to a draining replica. nginx cannot see readiness, so a
  browser that reconnects its SSE stream right after `event: reconnect` could be handed straight back to the draining replica (the
  drain test failed 2 of 5 runs that way). Fix: a draining replica refuses NEW streams with `503` + `X-Draining`, and the stream
  location retries a stream that never started (`proxy_next_upstream error timeout http_503`, 3 tries); once data has flowed a
  stream is never retried. Re-run: 6 of 6 drain runs passed through nginx. Ordinary requests still reach a draining replica during
  its grace period (by design: it answers them; in-flight and new ones complete, then it stops accepting and nginx retries the
  refused connection). **A's real `/stream` must do the same** (R32). The drain test originally used a plain GET as its "reconnect",
  which hid this on one edge and wrongly failed on the other; it now opens a new stream.
* A dead upstream used to produce a 60 s hang and an HTML 504; now 2 s and a JSON 503.

## What this does NOT give you
* **Active health checks**: after a replica dies, up to `max_fails` requests may be tried against it before nginx skips it (each is retried
  elsewhere if nothing was sent). Open-source nginx cannot do better; use a load balancer with active checks in production.
* **`server_now` on a cache HIT is up to 1 s old.** Harmless for fairness (arrival time is irrelevant to the lottery); it can skew a
  countdown by under a second.
* **A single worker process** (the Windows build of nginx): fine for the demo, not a capacity figure. Numbers measured here are
  relative; a Linux nginx on separate machines behaves differently.
* **Not a WAF**, not TLS (add it at the real deployment; the security headers assume HTTPS in production), no HTTP/2 locally.
* **A third-party CAPTCHA widget** needs its origins in `fd-security-headers.conf` (`script-src` and `frame-src`
  `https://challenges.cloudflare.com` for Turnstile). With the mock provider nothing is needed. The Turnstile widget itself was not
  tested in a browser (needs a real site key and hostname).
