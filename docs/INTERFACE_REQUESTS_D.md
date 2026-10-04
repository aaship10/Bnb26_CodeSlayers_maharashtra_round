# Interface requests from Member D (frontend)

Things the frontend assumes or needs from the other members. Each item says what the UI does **today** (against the mock) so nobody has to guess what breaks. Edit in place: mark items `DONE` or `CHANGED` with a date and the real shape.

## Member A (core engine)

| # | Request | Why / what the UI does meanwhile |
|---|---------|----------------------------------|
| A1 | `GET /events/{id}/status` returns `public_id` once the draw has completed (any entered user), and `ticket_code` when `state = CLAIMED`. | The fairness page highlights "your" result by `public_id`; the ticket page must survive a refresh, and `ticket_code` is otherwise only in the claim response. Both are optional in the zod schema. |
| A2 | `GET /events/{id}/stream` authenticates from **headers** (`Authorization` or `X-User-Id`, `X-Device-Id`) and honours `Last-Event-ID`. | Browser `EventSource` cannot set headers and we will not put tokens in URLs. The client reads SSE with `fetch` + a stream reader and sends `Last-Event-ID` itself. |
| A3 | SSE wire format: `id: <integer>`, `event: status`, `data: <same JSON as /status, incl. server_now>`; heartbeat comment at least every 20 s; `retry: 3000`. On reconnect with `Last-Event-ID`, replay every status change after that id; with no id, send the current status once. | Implemented and tested in Stage 3 (`src/features/live/`). The client treats 45 s of silence (no events, no heartbeat comments) as a dead connection and reconnects, so the heartbeat is load-bearing. Unknown event names are ignored, so you can add more later. |
| A9 | Close the stream (or simply stop sending) once a user's outcome is final; the client also stops on its own when state is CLAIMED, LOST or EXPIRED, or phase is CLOSED. | Saves 50,000 idle connections after the event. |
| A4 | `GET /events` shape: a JSON array of event objects, each with `server_now`. Fields used: `id, name, description?, phase, mode, inventory, window_opens_at, window_closes_at, claim_ttl_s, seed_commitment, server_now`. | Assumed, not in the contract. If you return an envelope (`{events, server_now}`) say so. |
| A5 | HTTP statuses for error codes (UI keys on `code`, never on status). Suggested: `WINDOW_NOT_OPEN` 409, `WINDOW_CLOSED` 409, `NOT_WINNER` 403, `HOLD_EXPIRED` 410, `ALREADY_CLAIMED` 409. | The mock uses these. |
| A6 | `POST /events/{id}/claim` replays the original response (same status and body) for a repeated `Idempotency-Key`. | The client reuses the key after refreshes and retries; it relies on this to never show two different tickets. |
| A8 | `POST /events/{id}/enter` stays idempotent on repeat calls (`already_entered: true`) even while the same user also holds a solved challenge. | The client auto-retries enter after network failures and 5xx, and re-sends the identical request after solving a challenge. |
| A10 | `GET /admin/events` (all events, including DRAFT) and `GET /admin/events/{id}`, each event = the public event fields + `config: { defences }`. | Not in the contract; the organizer list and control page need them because the public `/events` hides drafts. The mock implements this shape. |
| A11 | Lifecycle endpoints return the updated event (same shape as A10). Transitions: schedule DRAFT→SCHEDULED, open SCHEDULED→OPEN, close OPEN→DRAWING, draw DRAWING→CLAIMING; anything else 409 with `details: { phase, required }`. CLAIMING→CLOSED happens by itself when holds run out. What does `draw` do for FCFS events? | The UI only enables the button for the right phase but shows your 409 message verbatim. |
| A12 | `GET /admin/events/{id}/stats` shape: `{ event_id, phase, by_state: { REGISTERED, ENTERED, WON, WAITLISTED, CLAIMED, EXPIRED, LOST }, entrants, allocations: { inventory, claimed, held, available }, holds: { active, expired }, server_now }`. | Assumed. Add `synthetic: true` only if numbers are simulated; the UI then shows the MOCK badge. |
| A13 | `GET /admin/events/{id}/invariants` shape: `{ oversold, duplicate_users, duplicate_seats, orphaned_holds, passed, checked_at }`. | The badge is green only if `passed` AND every counter is 0. |
| A14 | All admin writes (`POST /admin/events`, lifecycle, `PATCH config`, `reset`) honour `Idempotency-Key` by replaying the first response. | The UI sends a fresh key per confirmation and reuses it if that confirmation is retried. |
| A15 | **docs/DRAW_SPEC.md with test vectors.** Until then the verifier follows `docs/DRAW_SPEC_PROVISIONAL.md`. Please confirm or correct each row: weight text form, entrant line format and sort order, `final_seed` byte layout (raw bytes vs hex), HMAC key and message, the weighted-key formula and `lnBig`, tie-breaks, and the `winners_hash`/`waitlist_hash` text. Also the `/fairness`, `/fairness/entrants` and (please add) `/fairness/results` shapes. | Everything is isolated in `drawSpec.ts`; swapping in your spec is a two-file change plus your vectors. |
| A16 | Audit chain encoding (hash preimage, canonical JSON rules, genesis value) with vectors, plus the `/audit` and `/audit/verify` shapes in the provisional doc. Please keep payloads free of floats, or define their text form. | Isolated in `auditSpec.ts`. |
| A17 | `public_id` on `/status` after the draw (see A1) is what lets attendees find themselves on the fairness page. It travels there in router state, never in a URL. | |
| A7 | Before the draw completes, `/status` must not include rank, `waitlist_position`, `seat_no`, `hold_expires_at` or `public_id`. | Hard rule on our side; the schema has no field to put them in, but the server must not send them either. |

## Member B (identity, defences, infra)

| # | Request | Why / what the UI does meanwhile |
|---|---------|----------------------------------|
| B1 | nginx: SPA fallback (`try_files $uri /index.html`) for everything except `/api/` and `/sim/`. | Client-side routes such as `/events/<id>/status` must survive a hard refresh. |
| B2 | nginx: for `/api/events/*/stream`, `proxy_buffering off`, `proxy_read_timeout` of at least 1 h, `X-Accel-Buffering: no`. | Otherwise SSE is held back and every client falls into polling. |
| B3 | `docs/POW_SPEC.md` with test vectors and a JS reference solver. | Until then the worker follows the rule in the shared conventions and uses my own vectors. |
| B4 | `docs/CONFIG_SCHEMA.md` (JSON Schema for `events.config.defences`) and the `GET /admin/defence/presets` shape (assumed: `[{ id, name, description, defences }]`). | The organizer config editor uses a provisional zod schema (`{ preset, layers: { rate_limit, pow, captcha, signals, risk } }`, each `{ enabled, ...params }`, params passed through untouched). Layer parameter names in the mock are guesses. |
| B5 | Which endpoints can answer `CHALLENGE_REQUIRED`? (assumed: enter, claim). Does a solved challenge cover one request or a time window? | The client retries the *same* request once per challenge and never loops silently; if a window applies it can skip re-solving. |
| B6 | Allow `X-Device-Id`, `Idempotency-Key`, `X-Challenge-Id`, `X-Challenge-Solution`, `X-Admin-Token`, `Last-Event-ID` through any header allow-list. | All are sent by the browser. |
| B7 | `Retry-After` plus `details.retry_after_ms` on every 429. | Used for the on-screen "try again in N seconds" and for backoff. |
| B8 | Which CAPTCHA provider will the stack use (hCaptcha, Turnstile, reCAPTCHA)? And what is the **accessible alternative** for people who can't use it? | Only a `mock` provider is wired; any other provider shows "This check isn't available here". The widget text promises organisers will help, so that route has to exist. |
| B9 | PoW difficulty: what `difficulty_bits` will be used in production, and is it per-request or adaptive? | The solver handles any value; measured about 1.2M attempts/s on a desktop browser worker (18 bits is about 0.2 s there, expect 5 to 10x slower on a mid-range phone; run `/__dev/pow-bench` on a real phone). |
| B11 | **nginx:** the proposed config in `frontend/deploy/nginx/` (with `frontend/deploy/README.md`) covers B1 and B2 plus caching, gzip and the security headers. It's tested on nginx 1.28 with the full e2e suite. Please merge it into `infra/` and rename the upstreams. | Until then the same layout runs through `vite preview`. |
| B12 | **CSP:** `script-src 'self'` and `font-src 'self'` are enforced. Anything B adds that loads third-party code (a CAPTCHA, analytics) needs its origin listed. | Tested: zero violations on the main screens. |
| B10 | Prefix length: is it bounded? | The solver is correct for any length (multi-block tested up to 130 bytes), but long prefixes cost a little more per attempt. |

## Member C (simulator)

The mock in `frontend/mock-server/src/sim.ts` implements everything below. Point `SIM_URL` at your service to test the real thing.

| # | Request | Why / what the UI does meanwhile |
|---|---------|----------------------------------|
| C1 | `GET /sim/runs`: recent runs, newest first, each with `run_id, scenario_id, scenario_name, status, progress, params (resolved), repeats, seed, target, created_at, finished_at`. | Not in the contract. The panel's "Recent runs" list and the FCFS vs Fair Drop pickers need it; without it they'd have to remember run ids in the browser. |
| C2 | Run stream (`/sim/runs/{id}/stream`) events: `status` (same JSON as `GET /sim/runs/{id}`), `progress` `{status, progress, phase_text}`, `snapshot` `{t_s, requests, throughput_rps, p95_ms, bot_seat_share}`; integer `id:` lines, and `Last-Event-ID` resume. | Assumed. Unknown event names are ignored; the client falls back to polling `GET /sim/runs/{id}` after 3 stream failures. |
| C3 | Which result fields are `Stat` and which are plain numbers? The UI accepts either for `system.*`, `detection.*` and `attacker_cost_per_seat.*`. A plain number is labelled "single value, no CI reported". `null` means not applicable (e.g. detection with no defences). | The fairness block is required to be all `Stat` (the brief says never a bare mean). |
| C4 | `POST /sim/runs` with `target: "real"` while the real stack isn't reachable: please answer `409 { code, message }` with a sentence we can show as-is. | The mock does this. |
| C5 | Chart datasets: `x` may be numbers or strings; numeric x spanning ≥ 100× is drawn on a log axis. If you want to force a scale, add `x_scale: "log" | "linear"` and we'll honour it. | |
| C6 | `scale` in `/sim/scenarios` is free-form; the UI shows it when it's a string. | |
| C7 | `docs/sample_results/` with a few real result files when you have them. | The UI is tested against the mock's results, which follow the documented schema exactly. |
