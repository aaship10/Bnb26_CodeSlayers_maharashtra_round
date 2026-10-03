# Interface requests from Member D (frontend)

Things the frontend assumes or needs from the other members. Each item says what the UI does **today** (against the mock) so nobody has to guess what breaks. Edit in place: mark items `DONE` or `CHANGED` with a date and the real shape.

## Member A (core engine)

| # | Request | Why / what the UI does meanwhile |
|---|---------|----------------------------------|
| A1 | `GET /events/{id}/status` returns `public_id` once the draw has completed (any entered user), and `ticket_code` when `state = CLAIMED`. | The fairness page highlights "your" result by `public_id`; the ticket page must survive a refresh, and `ticket_code` is otherwise only in the claim response. Both are optional in the zod schema. |
| A2 | `GET /events/{id}/stream` authenticates from **headers** (`Authorization` or `X-User-Id`, `X-Device-Id`) and honours `Last-Event-ID`. | Browser `EventSource` cannot set headers and we will not put tokens in URLs. The client reads SSE with `fetch` + a stream reader and sends `Last-Event-ID` itself. |
| A3 | SSE wire format: `id: <integer>`, `event: status`, `data: <same JSON as /status, incl. server_now>`; heartbeat comment at least every 20 s; `retry: 3000`. | This is what the mock emits and the Stage 3 client parses. Tell me if you want a different shape. |
| A4 | `GET /events` shape: a JSON array of event objects, each with `server_now`. Fields used: `id, name, description?, phase, mode, inventory, window_opens_at, window_closes_at, claim_ttl_s, seed_commitment, server_now`. | Assumed, not in the contract. If you return an envelope (`{events, server_now}`) say so. |
| A5 | HTTP statuses for error codes (UI keys on `code`, never on status). Suggested: `WINDOW_NOT_OPEN` 409, `WINDOW_CLOSED` 409, `NOT_WINNER` 403, `HOLD_EXPIRED` 410, `ALREADY_CLAIMED` 409. | The mock uses these. |
| A6 | `POST /events/{id}/claim` replays the original response (same status and body) for a repeated `Idempotency-Key`. | The client reuses the key after refreshes and retries; it relies on this to never show two different tickets. |
| A8 | `POST /events/{id}/enter` stays idempotent on repeat calls (`already_entered: true`) even while the same user also holds a solved challenge. | The client auto-retries enter after network failures and 5xx, and re-sends the identical request after solving a challenge. |
| A7 | Before the draw completes, `/status` must not include rank, `waitlist_position`, `seat_no`, `hold_expires_at` or `public_id`. | Hard rule on our side; the schema has no field to put them in, but the server must not send them either. |

## Member B (identity, defences, infra)

| # | Request | Why / what the UI does meanwhile |
|---|---------|----------------------------------|
| B1 | nginx: SPA fallback (`try_files $uri /index.html`) for everything except `/api/` and `/sim/`. | Client-side routes such as `/events/<id>/status` must survive a hard refresh. |
| B2 | nginx: for `/api/events/*/stream`, `proxy_buffering off`, `proxy_read_timeout` of at least 1 h, `X-Accel-Buffering: no`. | Otherwise SSE is held back and every client falls into polling. |
| B3 | `docs/POW_SPEC.md` with test vectors and a JS reference solver. | Until then the worker follows the rule in the shared conventions and uses my own vectors. |
| B4 | `docs/CONFIG_SCHEMA.md` (JSON Schema for `events.config.defences`). | The organizer config editor validates against it in Stage 4. |
| B5 | Which endpoints can answer `CHALLENGE_REQUIRED`? (assumed: enter, claim). Does a solved challenge cover one request or a time window? | The client retries the *same* request once per challenge and never loops silently; if a window applies it can skip re-solving. |
| B6 | Allow `X-Device-Id`, `Idempotency-Key`, `X-Challenge-Id`, `X-Challenge-Solution`, `X-Admin-Token`, `Last-Event-ID` through any header allow-list. | All are sent by the browser. |
| B7 | `Retry-After` plus `details.retry_after_ms` on every 429. | Used for the on-screen "try again in N seconds" and for backoff. |
| B8 | Which CAPTCHA provider will the stack use (hCaptcha, Turnstile, reCAPTCHA)? And what is the **accessible alternative** for people who can't use it? | Only a `mock` provider is wired; any other provider shows "This check isn't available here". The widget text promises organisers will help, so that route has to exist. |
| B9 | PoW difficulty: what `difficulty_bits` will be used in production, and is it per-request or adaptive? | The solver handles any value; measured about 1.2M attempts/s on a desktop browser worker (18 bits is about 0.2 s there, expect 5 to 10x slower on a mid-range phone; run `/__dev/pow-bench` on a real phone). |
| B10 | Prefix length: is it bounded? | The solver is correct for any length (multi-block tested up to 130 bytes), but long prefixes cost a little more per attempt. |

## Member C (simulator)

Nothing yet. Stage 6 will consume `docs/sample_results/` and `/sim/*` as specified.
