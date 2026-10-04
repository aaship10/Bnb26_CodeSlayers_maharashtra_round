# Fair Drop simulator (Member C)

Bot simulator, legitimate-crowd generator, open-loop load engine, experiment runner, metrics and charts that produce the evidence for Fair Drop. Python ≥ 3.11.

> "50,000 users" in this project means **50,000 logical users**, each a scheduled async client multiplexed over a bounded connection pool from one machine. It does **not** mean 50,000 simultaneous sockets.

## Setup

```powershell
cd simulator
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"          # add ,db for Postgres analytics (Stage 7)
pytest
```

## Commands

| Command | What it does |
|---|---|
| `fdsim mock [--port 8200]` | In-memory **mock** Fair Drop target (development double, never used for final results) |
| `fdsim smoke [--base-url http://127.0.0.1:8200]` | Walks one event through create → open → enter → close → draw → claim → invariants → verify |
| `fdsim load SCENARIO.yaml [--run-index N] [--users N] [--procs N]` | One open-loop run: schedules the window, runs the crowd, closes and draws at the planned time, then checks invariants and the draw. Prints a summary and writes `results/runs/<run_id>/` (`run.json`, `users.csv.gz` with ground-truth labels, `recorder.json` HDR histograms, `decisions.json`) |
| `fdsim serve [--port 8100]` | The `/sim` HTTP service the dashboard drives (see "The /sim service" below). Starts its own dev mock for `target: mock` |
| `fdsim experiment SPEC.yaml [--repeats N] [--allow-mock]` | One experiment (cells x repeats -> Results + chart dataset) |
| `fdsim suite [--only E1,E2] [--allow-mock]` | Every experiment E1..E8 from scratch with its fixed seed, regenerating every chart |
| `fdsim samples` | Regenerates the SYNTHETIC `docs/sample_results/` (results, charts, experiments, JSON Schemas) |
| `fdsim schemas` | Exports only the JSON Schemas |
| `fdsim validate FILE...` | Validates Results, chart, experiments JSON or scenario YAML |

Mock defaults: `AUTH_MODE=dev`, `SIMULATION_MODE=true`, `SIM_KEY=dev-sim-key`, `ADMIN_TOKEN=dev-admin-token` (override via env).

## Layout

```
fairdrop_sim/
  models/       pydantic: Scenario (YAML), Results (schema_version 1), ChartDataset, Experiment
  adapters/     the ONLY place with assumptions about A's and B's schemas      (stage 2/7)
  crowd/        arrival models, human behaviour, NAT groups                     (stage 2)
  engine/       open-loop scheduler, HDR latency, process sharding              (stage 2)
  bots/         9 attack profiles + cost accounting                             (stage 3)
  challenge/    PoW solver (done), CAPTCHA cost model                           (stage 3)
  metrics/      stats (Wilson, bootstrap: done), fairness/system/detection/integrity (stage 4)
  runner/       seeds, repeats, ablations, invariants + verifier; chaos in stage 7
  charts/       PNG renderer (fixed slot colours, watermarks) + dataset JSON    (done)
  service/      FastAPI :8100 under /sim and /, SSE, run manager, catalogue    (done)
  seeds.py      master seed -> named sub-seeds
  samples.py    SYNTHETIC sample data for D
mock_server/    in-memory target: LOTTERY + FCFS, seeded weighted draw, holds/waitlist, toy defences
experiments/    scenario YAMLs (E1..E8 arrive in stage 5)
tests/
```

## Mock target: what it is and isn't

It implements the shared contract: public `/events*`, `/defence/challenge`, all admin endpoints, `/admin/sim/tokens`, `X-User-Id` / Bearer / `X-Sim-Client-IP` with `X-Sim-Key`, idempotent enter, idempotent claim, holds, strict-order waitlist promotion, invariants, and a seeded draw that doesn't depend on arrival order and is verifiable via `/__mock/events/{id}/verify`. Its defence layers are **toys** (token buckets, PoW and CAPTCHA challenges, risk by identities per IP). They exist to exercise client code paths, not to model B's system. Analytics read `/__mock/events/{id}/export`, which stands in for the read-only Postgres queries on the real target. It contains no `sim_label`: ground truth stays in the simulator.

Results produced against it are always `target: "mock"`. Final experiments refuse to run against the mock without `--allow-mock` (Stage 5).

## Metrics engine (Stage 4)

`fdsim metrics <run_dir>...` aggregates repeats of one scenario into a `Results` JSON
(schema_version 1, the shape D consumes), with a 95% CI and n on every fairness number.

```bash
fdsim load experiments/attack_small_mock.yaml --run-index 0   # repeat
fdsim load experiments/attack_small_mock.yaml --run-index 1
fdsim load experiments/attack_small_mock.yaml --run-index 2
fdsim metrics results/runs/attack_small_mock-r0*  --out results/attack_small_agg.json
```

Modules in `fairdrop_sim/metrics/` (every function tested on hand-built fixtures with
known answers, `tests/test_metrics.py`):

- **stats.py** - Wilson interval for proportions, percentile bootstrap for means
  (10,000 resamples, seeded), ratio-of-sums bootstrap for cost-per-seat, bootstrap over
  identities for Jain/Gini, Spearman + point-biserial correlation, and a permutation test.
- **fairness.py** - bot seat share vs the lottery-neutral entrant share, human win
  probability, human entry success (captures lockouts), arrival-order correlation + a
  pooled permutation p, and Jain/Gini win-frequency spread over the fixed population.
- **system.py** - pooled HDR latency percentiles per endpoint and class, error rates
  (429 split legit/bot), throughput, availability.
- **detection.py** - precision / recall / false-positive rate (incl. NAT users) of the
  risk layer vs ground truth.
- **integrity.py** - worst case across runs; any violation makes the whole result red.
- **aggregate.py** - wires run artifacts into the `Results` model.

A `Stat` always carries a real CI; fields that can lack one are emitted as a bare number
(`system.*`, `detection.*`, `attacker_cost_per_seat.*`), and `null` means not applicable.
The real aggregate validates against D's own zod schema (`tests/test_frontend_contract.py`).

## Charts and the /sim service (Stage 6)

### Charts
`fairdrop_sim/charts/render.py` turns a chart dataset (the JSON is the table-view twin) into a PNG.
Rules it enforces: one validated categorical palette applied in a **fixed slot order, so a series keeps
its colour wherever it appears** (FCFS is always orange, "no defences" blue); marker shapes as a secondary
encoding (the aqua and yellow slots sit below 3:1 contrast on the light surface); a legend for two or more
series plus direct end-labels; CI bands on numeric x, dodged whiskers on categories; one y-axis, solid
hairline gridlines; **never more than 8 series** (a ninth is refused, not invented). Mock and synthetic data
are watermarked (diagonal text plus a red corner badge); a real result has neither. Two experiments can share
a `chart_id` (E5/E7, E6/E8), so a chart is always addressed as `(chart_id, experiment)`.

Chart kinds in an experiment spec: `metric` (default, one metric over the sweep), `multi_metric` (several
metrics over the same cells, e.g. E4 precision/recall/FPR) and `latency` (pooled p50/p95/p99, e.g. E3; no CI
is drawn because it is one merged histogram, and the notes say so). A cell whose metric is undefined (total
lockout) gets no point, and the chart's notes say `NO POINT for: ...`.

### The /sim service
`fdsim serve` (port 8100). Routes are mounted at `/sim/...` **and** at the root, so it works whether nginx
strips the prefix or forwards it (D's Vite dev proxy does not strip). Point the dev proxy at it with
`SIM_URL=http://127.0.0.1:8100 npm run dev`.

| Route | Notes |
|---|---|
| `GET /scenarios` | `flash_crowd`, `bot_swarm`, `sybil_farm`, `replica_kill`: the ids and flat params D's panel already sends |
| `POST /runs {scenario_id, overrides, repeats, seed, target}` | 201 `{run_id}`. 400 `VALIDATION_ERROR`; 409 `TARGET_UNAVAILABLE` (real stack down) or `SCENARIO_UNAVAILABLE` (chaos, until B's scripts) |
| `GET /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `GET /runs/{id}/results` | newest first; results 409 until done |
| `GET /runs/{id}/stream` | SSE: `status`, `progress`, `snapshot`; integer ids; `Last-Event-ID` resume (`0` = full history); `retry: 2000`; heartbeat comment every 15 s |
| `GET /experiments`, `GET /experiments/{id}` | real experiments from `results/experiments/*/experiment.json`, plus the **synthetic** samples (ids `synthetic-*`) unless `SIM_INCLUDE_SAMPLES=0` |
| `GET /charts/{chart_id}?experiment=` and `/charts/{chart_id}.png?experiment=` | dataset JSON / watermarked PNG |

One run executes at a time (concurrent runs would share the machine and distort each other's latency); the
rest queue. Each run is `repeats` independent runs (fresh reset and a fresh server seed each), aggregated into
one Results with CIs. Runs and results persist under `results/service/` and survive a restart (a run that was
in flight is marked failed: "service restarted"). Environment: `FD_MOCK_URL` (use a mock you already started),
`FD_REAL_URL` (default `http://127.0.0.1:8000`), `ADMIN_TOKEN`, `SIM_KEY`, `SIM_RESULTS_DIR`.

### Demo presets and their scale (read this before quoting a number)
Demo runs are **time-compressed** so a preset finishes in about two minutes: 2,000 logical users, 100 seats, a
6 s entry window, PoW at 10 bits, human CAPTCHA about 1 s. Request rates are not compressed. The defence layers
on the mock are toys. These are wiring checks, not evidence. Measured on this laptop against the **mock**
(2 repeats, seed 42; a 5-repeat run takes about 90 s):

| Preset (D's panel) | Bot seat share | Bot entrant share | Human win prob. |
|---|---|---|---|
| 1. FCFS under attack (1,000 bots, 100x) | **1.000** [0.981, 1.000] | 0.342 | 0.000 |
| 2. Fair Drop, same attack | 0.421 [0.353, 0.491] | 0.354 | 0.028 |
| 3. Bots 1000x faster | 0.417 [0.351, 0.487] | 0.364 | 0.029 |
| Sybil, 200 identities, defences off | 0.097 [0.063, 0.146] | 0.091 | 0.044 |
| Sybil, 200 identities, defences on | 0.005 [0.001, 0.028] | 0.014 | 0.049 |

Reading it: under FCFS the bots take every seat with a third of the entrants; under the lottery they get about
their entrant share, and 10x more request volume does not move it. Volume is not free for people, though: human
entry success fell to 87% at 1000x. With defences on, bots nearly vanish and humans pay for it (entry success
90.6%) because the mock's challenges are blunt. Following the team's decision (option A) the FCFS demo uses at
least 1,000 bot identities, more than the seats, so the near-100% claim is reachable and honest.

## Experiment runner (Stage 5)

```bash
fdsim experiment experiments/E5.yaml --allow-mock [--base-url URL] [--repeats N] [--out DIR]
```

An experiment spec (`fairdrop_sim/runner/experiment.py`, `ExperimentSpec`) is a **base
scenario** (a YAML path relative to the spec, or an inline dict) plus a grid of cells:

- `kind: sweep` - an `x` axis (`label`, dotted `param`, `values`, `scale`) crossed with named
  `series`, each a dict of dotted-path `overrides` (`attackers.0.identities`,
  `event.defences.preset`, ...; list items by index). `kind: runs` - one cell per series, no x.
- `metric` - a Results path read off each cell (`fairness.bot_seat_share`); a Stat gives the
  point its CI and n. Optional `baseline` draws a flat reference series from another metric
  (e.g. the lottery-neutral `fairness.bot_entrant_share`).
- `repeats`, `final`, `chart_id`, `y_label`, `title`, `description`.

Each cell runs `repeats` independent runs (same seed across cells, so a sweep is a paired
comparison). Every run goes through the invariants check and the draw verifier; the cell is
aggregated with pooled-Wilson CIs into `<out>/<id>/<cell>.results.json`, and the chart
dataset lands in `<out>/<id>/<chart_id>.json` (validated against D's `chartSchema` in
`tests/test_experiment.py`). Any failed integrity check turns the experiment red.
A spec with `final: true` refuses a `target: mock` base unless `--allow-mock` is given.

| Spec | Chart | Notes |
|---|---|---|
| E1 | `bot_share_vs_request_multiplier` | FCFS vs Fair Drop |
| E2 | `bot_share_vs_identities` | Sybil boundary x presets |
| E3 | `latency_percentiles_normal_vs_attack` | latency percentiles; needs special chart assembly (later stage) |
| E4 | `detection_precision_recall_by_layer` | needs special chart assembly (later stage) |
| E5 | `ablation_bot_share` | each defence preset + neutral baseline |
| E6 | `human_win_prob_under_attack` | human win probability under Sybil attack |
| E7 | `ablation_bot_share` | placeholder: needs B's `infra/chaos` scripts and the real multi-replica stack; Stage 7 `chaos_run.py` will wrap it |
| E8 | `human_win_prob_under_attack` | uniform vs spike arrivals; needs special chart assembly (later stage) |

Specs run at mock scale by default (`base_exp_mock.yaml`); each description gives the
full-scale numbers for the real target in Stage 7.

## Attack profiles (Stage 3)

Nine profiles in `fairdrop_sim/bots/`. A profile is a *behaviour* (timing + flags); its
*size* (`identities`, `ips`, `rps_per_identity`, `request_multiplier`) comes from the
scenario, so experiments sweep size without editing behaviour. `solves_pow`,
`solves_captcha`, `obey_retry_after`, `shared_device` default to each profile's natural
value and can be overridden per attacker.

| profile | what it does |
|---|---|
| `naive_flooder` | one identity, thousands of requests/s (flood) |
| `speed_bot` | pre-warmed, every identity fires at t=0 exactly |
| `retry_spammer` | many identities, each re-entering repeatedly |
| `sybil_single_ip` | N identities from one IP |
| `distributed_botnet` | N identities across M IPs (via `X-Sim-Client-IP`) |
| `human_mimic` | human-like timing, really solves PoW and models CAPTCHA |
| `late_flooder` | hammers after the window closes (tests `WINDOW_CLOSED` + shedding) |
| `window_edge_bot` | enters at the last instant (timing-equivalence check) |
| `claim_sniper` | floods status+claim through the claim phase to snipe promoted seats |

- **PoW is solved for real** (`challenge/solver.py` + `pow.py`), so the server verifies the
  nonce and every hash is counted. `pow_mode=modelled_delay` then waits `hashes/hash_rate`
  to model a device's compute time; `real` spends this machine's time. CAPTCHA is a model
  (`challenge/captcha.py`): wait a solve time, return the mock token, or fail.
- **Cost accounting** (`bots/cost.py`): requests, identities, IPs, real PoW hashes and
  CAPTCHA solves are counted and converted to a *modelled* dollar figure per bot-won seat.
  The counts are real; the dollars are a model (no accounts are bought, no solver paid).
- Bots run through the same open-loop `Sender` as humans, tagged class `bot`, so latency
  and error rates split legit vs bot. Each identity's label stays simulator-side; the wire
  carries only what a real client sends.

Run a mixed attack: `fdsim load experiments/attack_small_mock.yaml` (or
`all_profiles_smoke.yaml` for one of every profile). The summary prints bot seat share vs
bot entrant share and per-profile cost per seat.

## Load engine notes (Stage 2)

- **Open-loop.** Arrivals are scheduled independently of responses. Latency is measured from the *intended* send time (`tests/test_engine_units.py::test_open_loop_latency_counts_queueing` proves queueing shows up). Each user's own actions are sequential (see `docs/METRICS.md` §3).
- **aiohttp, not httpx, on the hot path.** Measured here, httpx's async pool collapsed from 448 rps sequential to 76 rps at 500 in flight against the same server. aiohttp held 1.1k to 4.4k rps. httpx is used only for low-volume admin calls.
- **Windows timers.** `time.monotonic` and asyncio tick every 15.6 ms (a 2 ms sleep overshot by about 14 ms). Shards call `timeBeginPeriod(1)` (overshoot drops to about 1.4 ms), and all timestamps use `perf_counter`.
- **Keep-alive.** The client retires idle connections after 15 s, which must stay below the server's idle timeout. The mock uses 75 s, like nginx. Otherwise sockets get reused just after the server closed them and show up as spurious `CONN_ERROR`s.
- **Sharding.** `load.procs` worker processes (spawn-safe). Each rebuilds the population from the seed and owns every `procs`-th user. They share one `perf_counter` timeline.

### 50k baseline against the MOCK (2026-10-04, development data, not evidence)

`fdsim load experiments/baseline_50k_mock.yaml`: 50,000 logical users, a 60 s window, 60% of arrivals in the first 3 s (about 20k offered requests in the first second), 500 seats.

| | 4 procs / 4,000 in flight | **8 procs / 1,024 in flight (committed)** |
|---|---|---|
| requests | 181,221 | 148,409 |
| connection errors | 44,972 | 12,025 |
| scheduler lag p99 | 794 ms | 123 ms |
| enter service time p50 / p95 | 1,010 / 2,111 ms | 223 / 2,054 ms |
| enter open-loop p50 / p95 | 3.8 / 10.4 s | 7.9 / 18.2 s |
| availability during spike | 0.61 | 0.82 |
| human entry success | 0.99996 | 0.99994 |
| integrity / draw verified | passed / yes | passed / yes |

Reading it honestly:
1. The **single-process mock** sustains only about 1k rps on real endpoints, so a 20k/s spike queues for seconds. That's a property of the dev double, not of Fair Drop.
2. The remaining connection errors come from connection storms against Windows' small listen backlog, and human retries recover almost all of them.
3. Scheduler lag is still above the 50 ms bar while the mock competes for the same CPUs. The run is flagged, and E6 on the real stack must meet the bar or report the lag.
4. Even fully saturated, there was **zero overselling, zero duplicates, and the draw verified**.
