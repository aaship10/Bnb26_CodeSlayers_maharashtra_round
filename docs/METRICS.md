# Fair Drop metrics (draft, Stage 1)

Owner: Member C. This file is the definition of record for every number in a Results file or chart. The code in `simulator/fairdrop_sim/metrics/` must match it, and every function is tested against hand-built outcomes with known answers (Stage 4). If code and doc disagree, that's a bug.

## 0. Conventions

- **Run**: one execution of a scenario with one derived seed. A **result** aggregates `repeats` runs of the same scenario.
- **Stat** = `{mean, ci_low, ci_high, n}`. The CI is a 95% interval and is **always present**. `n` (≥ 1) is the number of independent observations behind the mean, which is usually the number of runs.
- **Value** = a Stat *or* a bare number. A bare number means "single value, no CI reported" (D's UI labels it that way). It is allowed only in `system.*`, `detection.*` and `attacker_cost_per_seat.*`. The **fairness block is Stat-only**, so scenarios need `repeats ≥ 2`. We never invent an interval and never publish a CI-less fairness mean.
- `null` means *not applicable*: detection with no defence layer on, cost per seat when bots won nothing, `draw_verified` for FCFS, and a latency percentile with zero requests of that kind.
- **CI methods**:
  - *Per-run metrics aggregated over runs*: percentile bootstrap of the mean over runs, 10,000 resamples, seeded from the scenario seed (so it can be reproduced).
  - *A single proportion from one pooled count* (for example a detection rate over all users of all runs): Wilson score interval, with `n` = number of trials.
  - *Ratio-of-sums metrics* (cost per seat): bootstrap over runs of `sum(numerator) / sum(denominator)`.
- **Classes**: every simulated user is either `human` or `bot`. The label lives only in the simulator (and in `users.sim_label` on the real target, which only C reads). It never changes what a client sends.
- **Failed runs** are counted in `failed_runs` and listed in `per_run` with `status: "failed"` and the error. They are excluded from the means, and the exclusion is visible. They are never dropped silently.
- **Data provenance**: every file carries `target` (`real` | `mock`) and `synthetic` (bool). Charts from mock or synthetic data carry a watermark.

## 1. Seat accounting

| Term | Definition |
|---|---|
| **draw winners** | Entries in state `WON` or `CLAIMED` right after the draw (lottery), or holds granted during the window (FCFS). Mock and export field: `draw_state = WON`. |
| **final seats** | `CONFIRMED` allocations when the event closes, which is state `CLAIMED` in the user-facing API. Promotions from the waitlist count. |
| **entrants** | Identities with an accepted entry (one per identity; repeated enters are idempotent). |
| **intending humans** | Simulated humans that tried to enter at least once. |

Fairness metrics use **final seats** unless they say otherwise. Draw-winner variants are reported alongside where they differ, for example when bots never claim.

## 2. Fairness

| Metric | Definition | Per | CI |
|---|---|---|---|
| `bot_seat_share` | bot final seats / all final seats (0 if no seats were confirmed; such runs are flagged) | run | bootstrap |
| `bot_entrant_share` | bot entrants / all entrants. This is the **lottery-neutral baseline**: under a uniform lottery, E[bot_seat_share] ≈ bot_entrant_share. | run | bootstrap |
| `bot_advantage` (derived) | bot_seat_share / bot_entrant_share. A value of 1 is neutral, above 1 means bots gain. | run | bootstrap |
| `human_win_prob` | human final seats / intending humans | run | bootstrap |
| `human_entry_success_rate` | humans with an accepted entry / intending humans. This captures lockouts from 429s, timeouts, failed challenges and window misses. | run | bootstrap |
| degradation | `human_win_prob(no attack) − human_win_prob(attack)`, same population and seed family. The same applies to `human_entry_success_rate`. | pair of results | bootstrap of the difference over paired runs |
| `arrival_time_correlation` | Spearman ρ between arrival order (`arrival_seq`, 1 = earliest) and the win indicator (draw winner), among entrants. A negative value means early arrivals win more. We also report the point-biserial r. Expected: ≈0 for the lottery, strongly negative for FCFS. | run | bootstrap |
| `arrival_time_perm_p` | Two-sided permutation test of ρ = 0 (10,000 label permutations), on the pooled runs | result | n/a (it's a p-value) |
| `jain_index` | Over a **fixed population** across R repeats: x_i = number of runs identity i won. J = (Σx)² / (N·Σx²), in [1/N, 1]. Note: even a perfectly fair lottery has J < 1 because of sampling variance. The reference value for a uniform lottery is computed analytically and reported next to it. | result | bootstrap over identities |
| `gini` | Gini coefficient of the same x_i. Same caveat about the fair-lottery reference. | result | bootstrap over identities |

### Attacker cost per seat

For each attacker, the simulator counts **requests sent**, **accounts used**, **distinct IPs**, **PoW hashes computed** (real attempts) and **CAPTCHA solves**. Cost per seat = Σ cost over runs / Σ bot final seats over runs (a ratio of sums), with a bootstrap CI over runs. When the bots win 0 seats in total, the value is `null` and a note reads "no seats won, cost is unbounded".
`usd_modelled` applies `AttackerCost` from the scenario: per account, per IP, per CAPTCHA, per 10⁹ hashes, per 10⁶ requests. **This is a model, not a measurement.**

Profiles and their knobs are in `fairdrop_sim/bots/`. PoW is always solved for real (the server verifies it), so `pow_hashes` is genuine work; the solve *time* is modelled as `hashes / hash_rate` when `pow_mode=modelled_delay`. CAPTCHA is fully modelled (solve time + failure rate + the mock token), so `captcha_solves` is real but the per-solve price and latency are assumptions. Threats to validity (no real Sybil identity cost, modelled device and CAPTCHA costs, single machine) are listed in FINDINGS.md at Stage 7.

## 3. System

- **Latency** is measured **open-loop**. Each request has an *intended send time* from the schedule. Latency = response received − intended send time, so client-side queueing counts and coordinated omission is avoided. We record into HDR histograms (1 µs to 60 s, 3 significant digits) per endpoint (`enter`, `status`, `claim`) and per class (`.legit`, `.bot`). Histograms are pooled across runs. p50, p95 and p99 are read from the pooled histogram, and `n` is the request count. Per-run percentiles are also kept in `per_run` for CI-bearing charts (E3 bootstraps per-run percentiles).
- **Timing model.** The load is open-loop *across* users: each user's first attempt goes out at its planned arrival time regardless of server state. *Within* a user, actions are sequential like a real person: the next action is planned from the previous response plus think or backoff time, so the user's own waiting is not counted as latency. Every send also includes a modelled one-way network delay (lognormal, median about 50 ms) that shifts the send time and is **not** counted in latency.
- **Scheduler lag** = actual wake-up − intended send time, recorded per request. A run whose lag p99 exceeds 50 ms gets a warning: the generator could not deliver the planned load precisely, and open-loop latencies then include client-side delay.
- **Service time** (response − actual send) is recorded next to open-loop latency so server time and queueing can be told apart.
- **Final seats** are counted at **run end** (`t_draw + claim_phase_s`, default 3 × claim TTL). Holds still open at that point are reported as `WON`, not as seats.
- `throughput_rps`: completed responses / wall-clock duration of the load phase, per run.
- **Error rates** (per run, as fractions of requests): `http_429_legit` and `http_429_bot` (429 / requests of that class), `http_5xx`, `timeout` (no response within `request_timeout_s`; connection errors are counted here too and broken out in `per_run`).
- `availability`: 1 − (5xx + timeouts) / requests, during the spike interval (the first `spike_window_fraction` of the window).

## 4. Detection

Sources: `defence.decisions` (B) and `entries.weight` / `entries.risk`, compared against the simulator's ground truth. Unit = **identity**.

- A positive prediction means any of: an entry with weight < 1.0 or weight 0 (excluded), a decision `REJECTED`, or (per layer) that layer acting on the identity. "Acting" means rate limiting it more than K times, challenging it without the challenge being solved, or down-weighting it.
- precision = TP / (TP + FP), recall = TP / (TP + FN), and `false_positive_rate` = FP / (all humans). `false_positive_rate_nat` is the same restricted to humans in NAT groups (shared IPs).
- Per-layer numbers come from **ablation** (E4/E5: the layer enabled alone) and from attributing decisions by `layer`.
- CIs: Wilson on pooled counts across runs, with `n` = identities × runs (we note that identities are reused across runs).

## 5. Integrity (any violation makes the whole result red)

After every run: A's `GET /admin/events/{id}/invariants` and the draw verifier (`python -m app.verify_draw <event_id>`, or `/__mock/events/{id}/verify` on the mock).

| Field | Definition | Required |
|---|---|---|
| `oversold` | max(0, holds + confirmed − inventory) | 0 |
| `duplicate_users` | identities with more than one entry, or more than one allocation | 0 |
| `duplicate_seats` | seat numbers assigned more than once (or out of range) | 0 |
| `orphaned_holds` | holds that are past expiry and not released, or holds after the event closed | 0 |
| `draw_verified` | the verifier reproduced the draw from the revealed seed (null for FCFS) | true |
| `passed` | all of the above, for **every** run | true |

Results report the **worst case across runs**. A single failing run sets `passed: false`, and that run is identified in `per_run`.

## 5b. Entry-only runs (an engine that cannot draw yet)

If the target has no draw endpoint (Member A's stage 2), a run is **ENTRY-ONLY**: the load, latency, availability
and entry numbers are real, the claim phase is skipped, and `run.json` says `entry_only: true`. No seat can be
allocated, so every seat-based number is undefined:

- `human_win_prob` in `run.json` is `null` (never a convincing `0.0`), and `bot_seat_share` is `null` (0/0).
- `build_results` / `fdsim metrics` **refuse** to aggregate entry-only runs (`IncompleteRun`), so no Results file,
  chart or dashboard number can come from one. The per-run `run.json` is the report.
- `integrity.draw_verified` is `null` unless a verifier is configured (`FD_VERIFY_CMD`); it is never a claimed `true`.

The integrity block of a real run comes from C's **independent** SQL check of A's invariants I1-I7 (oversold,
duplicate seats, duplicate users, state/allocation mismatch, orphaned holds, WON-without-hold, waitlist gaps),
and, once A ships `/admin/events/{id}/invariants`, **both** must pass. I8 (audit chain), I9 (recomputing the draw)
and I10 (window bounds) need A's code and are not covered by C's SQL.

## 5c. Chaos runs (E7)

`fdsim chaos` runs C's open-loop load while commands inject (and heal) a fault at chosen offsets. Requests are
bucketed by **completion** time at 100 ms resolution (bucketing by intended send time smears failures over the
seconds the requests were scheduled in and hides a dip; the first real chaos run exposed that).

| Field | Definition |
|---|---|
| `requests_failed` | 5xx + timeouts + connection errors over the run |
| `outage_seconds` | the longest stretch, from the fault on, with **no successful response** that contains at least one failure. Clients that back off leave silent buckets inside it, so silence counts; silence after recovery does not (the stretch ends at the next success). A partial failure (one replica of several) leaves successes between the failures, so it is not an outage |
| `recovered` | `false` if the run ended while that stretch was still open |
| `degraded_seconds` | whole seconds from the fault on whose failure share exceeds 5% |
| `recovery_seconds` | from the heal command finishing to the end of the last failure; `0` if failures stopped first; `null` if nothing failed |
| `invariants_passed` | the run's integrity block (see 5b). An availability dip is acceptable, an invariant violation never is |
| `ordering_ok` | a heal that *starts before the inject finished* is a race and fails the run |
| `steps_ok` | every fault command exited 0 and was due before the run ended |

Control-plane calls (open / close / draw) retry transport failures and 5xx for up to 45 s, so a run survives an
outage of the very target it is measuring. Every retry is counted in `run.json` (`control_retries`). Resolution is
0.1 s; one run is one observation, so a single chaos run is a demonstration, not a distribution.

## 6. Repeats and reporting rules

- 30 repeats at small scale (≤ 5k users) and at least 10 at 50k scale. Seeds come from `derive_seed(master, "run", i, ...)` and are recorded per run.
- No cherry-picking. Every run that started is in `per_run`.
- Every reported mean comes with its CI and n. Charts draw CI bands or error bars.
- Mock or synthetic data is watermarked "MOCK DATA" or "SYNTHETIC" and is never mixed into final charts.

## 7. Open questions (resolved in Stages 4–5)

- Jain and Gini reference values for a fair lottery: closed form or simulated?
- Should detection be scored per identity or per request? Per identity is the default. Per request is shown for rate limiting only.
- Final mapping of A's allocation states (`HELD`/`CONFIRMED`/`EXPIRED`) to the user-facing states. This is pending A's schema, see `docs/INTERFACE_REQUESTS_C.md`.
