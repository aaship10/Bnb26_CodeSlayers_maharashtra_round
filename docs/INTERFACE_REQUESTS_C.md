# Interface requests from Member C (simulator and evidence)

What the simulator assumes about the other members' work. The simulator's own mock target (`simulator/mock_server/`) implements each assumption below, so you can run it to see the exact shape. Owners: please edit in place and mark items `DONE` or `CHANGED` with a date and the real shape. All schema guesses on my side live in `simulator/fairdrop_sim/adapters/`, so a change here touches one file.

> **Deployment (2026-10-04):** A and B are not using Docker. The simulator targets the backend over `--base-url` (e.g. `http://127.0.0.1:8000`, or nginx `:8080` if B runs it) and adds no containers of its own.

## Member A (core engine)

| # | Request | Why / what the mock does meanwhile |
|---|---------|------------------------------------|
| A-C1 | `POST /admin/events/{id}/reset` wipes entries/allocations/audit and returns the event to SCHEDULED/OPEN with a **fresh** server seed. **RESOLVED 2026-10-04:** A generates the seed (commit-reveal); C does not choose it. Each repeat is an independent draw, which is what I want for confidence intervals. | Repeated runs for CIs; the draw is *verifiable* (not reproducible by C). |
| A-C2 | Timing control via **manual admin overrides** `open`/`close`/`draw` (A confirms these exist "for demos"). I set generous `window_opens_at`/`window_closes_at` at create, then drive the exact window with overrides so runs have precise, repeatable timing. Please confirm the worker won't also auto-close/draw and race my overrides (or give a flag to disable auto-advance per event). | Precise open-loop timing per run. |
| A-C3 | Draw outcome must depend only on (seed, set of (user, weight)), not on arrival order or timing. | The E8 timing-equivalence experiment tests exactly this. The mock uses weighted sampling without replacement keyed by HMAC(seed, event:user). |
| A-C4 | `GET /admin/events/{id}/invariants` → `{passed: bool, checks: {oversold, duplicate_users, duplicate_seats, orphaned_holds, ...: int}, server_now}`. | The runner fails a run when `passed` is false and records `checks`. |
| A-C5 | **CONFIRMED:** `python -m app.verify_draw <event_id>` exists (plus a pure public-input function). Please keep exit code 0 = verified, non-zero = not, and print JSON on stdout. The runner calls it after every lottery run; until integration I use the mock's `/__mock/events/{id}/verify`. | Integrity gate per run. |
| A-C6 | **RESOLVED from A's schema:** I read `entries(user_id, public_id, arrival_seq, entered_at, weight, risk, state, draw_rank, waitlist_position)`, `allocations(entry_id, seat_id, status in HELD/CONFIRMED/EXPIRED/RELEASED, hold_expires_at, confirmed_at, ticket_code)`. "Draw winner" = `draw_rank between 1 and inventory`; "final seat" = allocation `CONFIRMED`. One confirm needed: is `entries.state` the live state (ENTERED/WON/WAITLISTED/CLAIMED/EXPIRED/LOST)? | Fairness metrics; db_adapter real query joins these three tables. |
| A-C7 | A read-only Postgres role for C (or a DSN) with SELECT on `users`, `entries`, `allocations`, `events`, `defence.decisions`. | Analytics only, never writes. |
| A-C8 | `POST /admin/events` body: `{id?, name, inventory, mode, window_seconds, claim_ttl_seconds, config?}`. Optional client-chosen `id` helps reproducibility. | The mock accepts exactly this. |
| A-C9 | `FCFS` mode: entries in arrival order get holds immediately while seats remain; the rest queue and are promoted in arrival order when holds expire. `enter` returns `state: WON` or `WAITLISTED` in FCFS. | Baseline comparison. Tell me if your FCFS differs (for example "sold out", meaning no waitlist). |
| A-C10 | Error HTTP statuses as in D's A5 (`WINDOW_*` 409, `NOT_WINNER` 403, `HOLD_EXPIRED` 410, `ALREADY_CLAIMED` 409). | My client keys on `code`, but the stats split by status. |

## Member B (identity, defences, infra)

| # | Request | Why / what the mock does meanwhile |
|---|---------|------------------------------------|
| B-C1 | `POST /admin/sim/tokens {user_ids[]}` → `{tokens: {user_id: jwt}}`, at least 5k ids per call. | Auth mode `jwt_sim_tokens` for 50k users. |
| B-C2 | With `SIMULATION_MODE=true` and a valid `X-Sim-Key`: `X-Sim-Client-IP` replaces the client IP for **every** defence layer (rate limit, signals, risk). With an invalid key: 403 `FORBIDDEN`. | Distributed-botnet and NAT-group simulation from one machine. |
| B-C3 | **CONFIRMED** `GET /admin/defence/decisions?event_id=&cursor=` (cursor paging). Fields: `{event_id, user_id, ts, action, weight, score, signals, layer, ip, device}`, `action ∈ {ALLOW, REJECT, CHALLENGE}` (a down-weight is ALLOW with weight<1). Never includes sim_label. | Detection precision/recall; my detector keys on entry weight<1 plus action=REJECT. |
| B-C4 | `docs/POW_SPEC.md` vectors. My solver is already tested against D's vectors (`frontend/tools/ref_pow.py`). | Ensures all three implementations agree. |
| B-C5 | **Need the MockCaptcha token format.** B's MockCaptcha accepts tokens **signed with SIM_KEY** (not a fixed string). Please document the exact signing (HMAC? over what message: user_id+event_id? encoding?) so C's CAPTCHA-solving model emits a valid token against the real stack. My own mock still uses the fixed `mock-captcha-ok`; the real token is isolated in `challenge/captcha.py`. Also: is a solved challenge single-use, and does it cover the user for the event? | Bot CAPTCHA cost model + the challenge retry loop. |
| B-C6 | Chaos script CLI: `python infra/chaos/chaos_run.py --action <kill-backend|kill-redis|pg-failover|...> [--at-s N]`, exit code 0 on success. | E7 orchestrates it as a subprocess at t = X. |
| B-C7 | nginx `/sim/` → `simulator:8100`. Please say whether the `/sim` prefix is stripped. My service answers on both `/sim/...` and `/...`, so either works. Also SSE settings for `/sim/runs/*/stream` (`proxy_buffering off`, long read timeout). | D's Vite dev proxy does **not** strip `/sim`. |
| B-C8 | `GET /admin/defence/presets` → `[{id, layers: {name: {enabled, ...params}}}]`. | The runner records the fully expanded layers in every result. |
| B-C9 | Rate limit 429s include `details.scope` (`ip`/`user`/`global`). | Splits false positives on NAT groups from per-user limits. |

## Member D (frontend)

| # | Note | Detail |
|---|------|--------|
| D-C1 | Sample data is ready: `docs/sample_results/` | 4 Results files (the demo presets), 6 chart datasets (all chart ids), `experiments.json`, and JSON Schemas in `schema/`. Everything is `synthetic: true` and `target: "mock"`. Regenerate with `fdsim samples`. |
| D-C2 | Watermark rule | Show a visible "SYNTHETIC" badge when `synthetic` is true, and "MOCK DATA" when `target == "mock"`. Real final results have both off. |
| D-C3 | Nullable fields | A `Stat` always has numeric `ci_low`/`ci_high` (matches your `statSchema`). Values without a CI are bare numbers, and only where you accept `Stat | number`. `null` = not applicable: detection with no defences, cost per seat when bots won 0 seats, `draw_verified` for FCFS. Chart points **omit** `ci_low`/`ci_high` when there is no interval (they are never `null`). |
| D-C6 | Latency percentile with zero requests | **Request:** please accept `null` for `latency_ms.<endpoint>.p50/p95/p99` (it only happens when no request of that kind was sent, for example no claims because no winner claimed). Today your `pct` schema would reject the whole result. |
| D-C7 | Extra keys you can use | Results: `failed_runs`, `per_run[]` (`{index, seed, status, error, values}`), `notes[]`, `arrival_time_perm_p`, `attacker_cost_per_seat.usd_modelled`, `detection.false_positive_rate_nat`, `system.availability`, `latency_ms["enter.legit"|"enter.bot"]`, each with `n`. Charts: `x_scale` (`linear`/`log`/`category`), `y_scale`, `experiment_id`, `run_ids`, and `n` per point. |
| D-C10 | **Make five fairness fields nullable** | `arrival_time_correlation`, `jain_index`, `gini`, **`bot_seat_share` and `bot_entrant_share`** are genuinely undefined in degenerate runs: no entrants, no winners, or (the last two) a defence that locks every identity out so there are 0 seats / 0 entrants (a 0/0 share, which we refuse to report as a measured 0%). The engine emits `null` and explains it in `notes[]`. Your `resultsSchema` currently requires them as `statSchema`; please accept `statSchema.nullable()` and show the note instead of a number. All realistic results (and every sample file) still carry real Stats. Chart points for such cells are omitted and the reason is in the chart's `notes`. |
| D-C11 | **The real `/sim` service is up** (`fdsim serve`, port 8100) | Run it, then `SIM_URL=http://127.0.0.1:8100 npm run dev`. It answers at `/sim/...` and at the root, so nginx may strip or keep the prefix. Every payload (scenarios, run, run list, results, experiments, chart, snapshot) is checked against **your** zod schemas in `simulator/tests/test_service.py`. For `target: "mock"` it starts its own dev mock; no second terminal needed. |
| D-C12 | **Scenario params** | Same ids and param names as your mock (`flash_crowd`, `bot_swarm`, `sybil_farm`, `replica_kill`; `legit_users`, `bots`, `request_multiplier`, `inventory`, `mode`, `defence_preset`, `bot_identities`, `kill_at_s`). Defaults are **demo scale** (2,000 users, 100 seats, `bots` 1,000, per your option-A decision) and `scale` says so. One extra optional param: `sybil_farm.bot_ips` (integer, default 1: the whole farm shares one IP; raise it to see IP signals evaded). Your presets 1-4 work unchanged. |
| D-C13 | **Demo numbers** | Preset runs take about 90 s at 5 repeats. The measured demo-scale numbers are in `simulator/README.md` ("Demo presets and their scale"): FCFS preset 1 gives bots 100% of seats with 34% of entrants; Fair Drop preset 2 gives them about their entrant share. Those are **mock** runs: fill the `[FCFS bot share]` placeholders in `DEMO_SCRIPT.md` from a run you do on stage or from the pre-run pair, and leave the red MOCK badge on. Do not copy README numbers into slides as measurements. |
| D-C14 | **Charts are per experiment** | E5/E7 share `ablation_bot_share` and E6/E8 share `human_win_prob_under_attack`. Always request `/sim/charts/{chart_id}?experiment={id}` (your client already does); the experiment picks which dataset you get. Real experiments appear in `GET /sim/experiments` after `fdsim experiment` / `fdsim suite`; until then you get the `synthetic-*` samples. |
| D-C9 | **Demo script fix (decided 2026-10-04, option A)** | `DEMO_SCRIPT.md` §1 and `SLIDES_OUTLINE.md` say "200 bots take ~100% under FCFS". That can't happen: one seat per identity caps 200 bots at 200/500 = 40%. The FCFS demo preset and E1 will use **1,000 bot identities** (more than the 500 seats), so "near 100% under FCFS, about 2% under Fair Drop" is honest. Please change "200 bots" to "1,000 bots" in the script and slides. The bracketed numbers will come from the measured runs. |
| D-C8 | Contract test | `simulator/tests/test_frontend_contract.py` runs **your** zod schemas (`features/sim/schemas.ts`) over every file I generate. If you tighten a schema, my CI tells me. |
| D-C4 | Dev proxy | Run the simulator service (Stage 6) with `SIM_URL=http://127.0.0.1:8100 npm run dev`. Your default `/sim` target is your own mock on :8787. |
| D-C5 | Extra fields | Results may gain new optional fields within `schema_version: 1`. Keep your zod schemas non-strict (strip unknown keys), which they already are. |

## Integration probe against the real stack (2026-10-04, `origin/main` @ 10277c8)

C ran A's **real, unmodified backend** on a real Postgres (A's migrations 0001/0002) and drove it with the
simulator; B's package was read, not run (B's own docs say it has not been integrated into A). Everything below was
observed, not inferred. Reproduce with `simulator/tools/dev_real_stack.py` and `fdsim doctor` (see FINDINGS.md).

### For Member A

| # | Finding | What C needs |
|---|---------|--------------|
| A-P1 | **No draw, claim, reset, stats, invariants, stream or `/readyz` yet** (A's README says stage 2). After `close`, an event stays in `DRAWING` forever: entries remain `ENTERED`, `draw_rank` is NULL. | Until `draw` and `claim` exist, no run can allocate a seat, so C can publish **no fairness number** from the real engine. C's runner detects this (`fdsim doctor`), runs the entry path only, and refuses to aggregate it. |
| A-P2 | **FCFS entry answers `501 FCFS entry is not implemented yet`** (stage 5). | E1's FCFS arm cannot run against A until then. |
| A-P3 | **A's Windows launcher dies, it does not degrade, above ~500 concurrent sockets.** `app.serve` runs on `asyncio.SelectorEventLoop(selectors.SelectSelector())` (needed by psycopg async); Windows `select()` is capped at 512 file descriptors, and the process exits with `ValueError: too many file descriptors in select()`. Reproduced with 500 in-flight client connections plus the DB pool. B's `infra/local/serve.py` uses the identical loop. | Document it, and keep per-replica concurrency under ~400 (clients + DB pool + listener), or run several replicas behind a balancer. B's flash-crowd tests went through B's gateway, which probably hides it. On Linux this does not apply. |
| A-P4 | `app.serve` needs **Python >= 3.12** (`asyncio.run(..., loop_factory=)`); the rest of the backend compiles and runs on 3.11. | Say so in the README ("requires 3.12" is currently only in `pyproject`). |
| A-P5 | `GET /events` returns an **envelope** `{events: [...], server_now}`; D's request A4 and zod schema assume a bare array. | Decide which side moves, and tell D. C's test pins the envelope (`tests/test_real_target.py`). |
| A-P6 | Dev auth: a user id that is not in `users` gets `401 Unknown user` (so every simulated identity needs a real row). A **DRAFT** event answers `404` on public routes. | C provisions users by direct SQL (`sim_label` set, never read back). Nothing to change; recorded so nobody is surprised. |
| A-P7 | `entries` has no client IP and no per-entry request count. | Fine for fairness; B's IP-based signals will need their own store. |
| A-P8 | **Entry path, measured, with a caveat about the measurement.** 23 runs of a 2,000/5,000-user flash crowd on a single replica: **zero 5xx, zero timeouts**, every person told "entered" has exactly one entry and nobody else does, I1-I7 clean; the people not admitted were only ever turned away by `WINDOW_CLOSED` (requests still queued). But the **speed flipped about 10x between identical runs** on this laptop (enter service p50 81 to 89 ms in 5 runs, 841 to 1,230 ms in 15; 74 to 100% admitted) for reasons we could not isolate (ruled out: API process, data, fsync, port, stack age). | **No throughput figure can be taken from C's runs.** If A can reproduce a 10x swing on its own machine, that is worth a look (connection pool, Python scheduling, Windows hybrid-core behaviour); otherwise a second machine for the generator will tell us. FINDINGS.md section 3.1. |
| A-P9 | What held: **no acknowledged write lost** across a hard kill and restart of the only API process (entries in the DB equal users told "entered"), idempotent `enter` (`already_entered: true`, same `entered_at`), window enforcement (`WINDOW_NOT_OPEN` / `WINDOW_CLOSED`), no rank fields before the draw, and C's independent I1-I7 SQL checks clean. | Thank you; nothing to do. |

### For Member B

| # | Finding | What C needs |
|---|---------|--------------|
| B-P1 | **B's package is not wired into A's backend** (B's docs say so). `/admin/defence/*`, `/admin/sim/tokens`, the gate and rate limits do not exist on A's app today. | Until integration, real-target runs have defences OFF only. C's detection metrics (E4) and the defence ablations (E2, E5) cannot be run against the real stack. |
| B-P2 | **B-C5 answered.** MockCaptcha's simulator token is `sim1.<user32>.<event32>.<ts>.<mac32>` (HMAC-SHA256 under SIM_KEY, 300 s). C implemented `mint_sim_token` and checks it against **B's own function with golden vectors** (`tests/test_captcha_token.py`); the dev mock now accepts it too. | Please keep the format stable, or tell C. |
| B-P3 | **B-C3 answered:** decisions are `GET /admin/defence/decisions?event_id=&cursor=&limit=&action=`, keyset-paginated. C's driver pages it. | The page body shape is assumed to be `{items|decisions: [...], next_cursor}`; please confirm. |
| B-P4 | **R26 matters for C's results.** B's signals read `defence.identities` (registration device, IP, subnet, OTP latency). Simulated users without those rows get *neutral* identity signals, so a Sybil farm would look like a crowd of ordinary strangers. C has **not** provisioned those rows yet (B's schema is not in A's database). | When integrating, say which columns and what a *plausible* bot and human row look like. C will insert them, and will state that choice as a modelling assumption in FINDINGS (it encodes the attacker's registration behaviour). |
| B-P5 | The Windows `select()` ceiling (A-P3) also applies to B's `infra/local/serve.py`. | See A-P3. |
| B-P6 | B's `chaos.py` is a self-contained harness (own load, one fault, own checks), so it cannot run alongside C's load without doubling it. C's E7 instead fires **inject-only commands** (`python infra/local/run.py kill 8002`, then `heal`) at chosen offsets during C's load and measures the outage from C's own request timeline (`fdsim chaos`). | Confirm `run.py kill <port>` / `heal` return promptly and are safe to call from another process. B's own `chaos/results/*.json` stay B's evidence. |

### For Member D

| # | Finding | Action |
|---|---------|--------|
| D-P1 | A's `GET /events` is `{events, server_now}`, not a bare array (A-P5). | Your `eventListSchema` will reject the real response. Either A changes or your client unwraps. |
| D-P2 | `/sim/*` is real now (D-C11..14) and the real target answers a precise 409 when the engine is incomplete: `TARGET_UNAVAILABLE` with a sentence such as "The real engine ... is reachable but incomplete: it has no draw, claim endpoint(s) yet." | Show `message` as-is; do not retry. |

## Answers to D's requests (from `INTERFACE_REQUESTS_D.md`, section "Member C")

| D's # | Answer (2026-10-04) |
|---|---|
| C1 `GET /sim/runs` | **DONE 2026-10-04.** Exactly your fields (`run_id, scenario_id, scenario_name, status, progress, params, repeats, seed, target, created_at, finished_at`, plus `phase_text`, `started_at`, `error`), newest first. |
| C2 run stream | **DONE 2026-10-04.** `id:` integer lines, `Last-Event-ID` replay (`0` = full history), events `status`, `progress {status, progress, phase_text}` and `snapshot`. Heartbeat comment every 15 s, `retry: 2000`. **Snapshots are real measurements, not a model:** while a repeat runs they carry `{t_s, entries, repeat, requests?}` (about 1 per second); when a repeat finishes they carry that repeat's measured `{requests, throughput_rps, p95_ms, bot_seat_share?}`. So `bot_seat_share` and `p95_ms` appear once per repeat, not continuously. A fresh connection (no `Last-Event-ID`) gets the current status first, then new events, and ends when the run is terminal. |
| C3 Stat vs number | See D-C3 above. Fairness = always Stat. `system.*`, `detection.*`, `attacker_cost_per_seat.*` = Stat or a bare number, or `null` where not applicable. |
| C4 `target: "real"` unreachable | **DONE 2026-10-04.** `409 {code: "TARGET_UNAVAILABLE", message: "<sentence you can show as-is>"}`. A scenario that exists but cannot run yet (`replica_kill`, until B's chaos scripts) answers `409 {code: "SCENARIO_UNAVAILABLE", message}`. Both keep the form's `details.field`. |
| C5 chart x scale | **DONE.** Every dataset carries `x_scale` (`linear`, `log` or `category`) and `y_scale`. |
| C6 `scale` | A string, e.g. `"2,000 logical users + 200 bots"`. |
| C7 real files | They'll go into `docs/sample_results/` (with `synthetic: false, target: "real"`) after Stage 7's full runs. |
