# Fair Drop: simulation and evidence (Member C)

Written 2026-10-04 against `origin/main` @ `10277c8`. Read section 0 first.

## 0. Status, stated plainly

**The four claims we set out to prove are not yet proven on the real system, because the real engine cannot allocate a
seat yet.** What exists, and what does not:

| | State |
|---|---|
| The measurement machinery (open-loop load, 9 attack profiles, metrics with confidence intervals, experiments E1-E8, charts, `/sim` service, chaos orchestration, real-target adapters) | **Built and tested** (198 automated tests; 8 of them need A's real backend and skip without it) |
| Member A's backend | **Stage 2 only**: create, schedule, open, close, enter, status. **No draw, no claim, no reset, no invariants endpoint, FCFS entry returns 501.** After `close` an event stays in `DRAWING` forever |
| Member B's defences | **Not wired into A's backend** (B's own docs say so). Everything of B's was tested against B's stub |
| Fairness numbers from the real system | **None, and none can be produced yet.** The runner detects the missing draw, runs the entry path only, and refuses to aggregate it (section 3.1) |
| What *is* measured on the real engine | The entry path under a flash crowd (23 runs), and a kill-and-restart of the API under load (2 runs), on a development stack whose speed varied 10x for unidentified reasons (sections 1, 3.1, 3.2) |
| Everything about bots, FCFS vs the lottery, Sybil curves and detection | Measured **only on our in-memory mock**, which implements the same rules. That shows the harness works and what the rules *imply*; it is **not evidence about A's or B's code** (section 3.4) |

Nothing in this document should be quoted as a measurement of Fair Drop's fairness or speed. What can honestly be quoted: the
entry-path integrity results in 3.1 (no lost or duplicated entry, zero server errors, in 23 runs), the chaos result in 3.2, the
integration findings in 3.3, and, labelled as mock, the shapes in 3.4.

## 1. What was run, and on what

Everything ran on **one Windows 11 laptop**: Intel i5-13450HX (10 cores, 16 threads), 23.7 GB RAM. The load generator, the
API, and the database all shared it. That matters for every latency below (section 5).

| Component | Version / setting |
|---|---|
| Member A's backend | `origin/main` @ `10277c8`, **unmodified**, one replica |
| Python | **3.11.9** (A requires >= 3.12; its code compiles and runs on 3.11, but its launcher `app.serve` does not, so a 3.11-equivalent launcher was used: `simulator/tools/dev_real_stack.py`) |
| PostgreSQL | 16.2, bundled by the `pgserver` wheel, default tuning (`max_connections` 100, `shared_buffers` 128 MB, `fsync` on, `synchronous_commit` on) |
| Beacon | mock (A's `BEACON_PROVIDER=mock`) |
| Auth | A's dev mode (`X-User-Id`); users are real rows in A's `users` table, created by direct SQL with `sim_label` set and never read back |
| Defences | **none** (B is not integrated) |
| Simulator | `simulator/` @ this commit; mock target for everything that needs a draw |

Reproduce the real-target runs (no PostgreSQL install, no Python 3.12 needed):

```powershell
py -3.11 -m venv .venv_a
.venv_a\Scripts\python -m pip install --ignore-requires-python -e ..\backend[dev] pgserver "psycopg[binary]"
.venv_a\Scripts\python tools\dev_real_stack.py up --backend-dir ..\backend      # prints FD_REAL_URL and FD_REAL_DSN
fdsim doctor --base-url $env:FD_REAL_URL                                        # what is missing, and who owns it
fdsim load experiments\real_entry_small.yaml --base-url $env:FD_REAL_URL --run-index 0
```

## 2. Method

- **Open loop.** Each simulated user's first attempt is sent at its planned time whatever the server is doing; latency is
  measured from the *intended* send time, so a slow server cannot hide by making the generator wait (coordinated omission).
  A test proves queueing shows up (`test_open_loop_latency_counts_queueing`). Service time (from the actual send) and the
  generator's own scheduling lag are recorded next to it. A run whose scheduling lag p99 exceeds 50 ms is flagged, because
  its latencies then include client-side delay. **Most entry-path runs below are flagged** (lag p99 up to 78 ms).
- **"50,000 users" means 50,000 logical users**: scheduled asynchronous clients over a bounded connection pool on one
  machine, not 50,000 simultaneous sockets. The real runs here used 2,000 and 5,000 users.
- **Fixed seeds, recorded configs.** One master seed derives the population, arrivals and bot behaviour; every run records
  its scenario. A's draw is commit-reveal with a **fresh seed per event**, so repeats are independent draws (good for
  confidence intervals) and are verifiable rather than reproducible.
- **Statistics.** Proportions use the Wilson interval on pooled counts (sound at any number of repeats); other means use a
  10,000-resample percentile bootstrap; Jain and Gini are bootstrapped over identities; arrival-order correlation uses
  Spearman with a permutation test. A `Stat` always carries a real interval and `n`; a share that is 0/0 is `null`, never
  an invented 0%. Definitions: `docs/METRICS.md`, each tested on hand-built outcomes with known answers.
- **Ground truth never reaches the server.** Bots and humans send the same kinds of headers; the label lives only in the
  simulator. `sim_label` is written on provisioning and never read.
- **Integrity does not rely on the engine's own checker.** C recomputes A's invariants I1-I7 from the tables with plain SQL
  (`adapters/real_db.py`), and a run is red if either checker fails. The SQL was validated by planting violations in A's real
  schema and confirming they are caught (`tests/test_real_target.py`).
- **The runner refuses to publish what it cannot know.** If the target has no draw, the run is ENTRY-ONLY: seat-based
  numbers are `null`, and no Results file, chart or dashboard number can be built from it.

## 3. Results

### 3.1 Real engine: the entry path under a flash crowd (entry only)

A's real backend, one replica, 2,000 logical humans (10% behind shared NAT addresses; 30% refresh 1 to 3 times), 60% arriving
in the first 5% of a 10 s window, no attackers, no defences, 200 requests in flight. **20 runs** of the 2,000-user scenario and
3 of a 5,000-user scenario (30 s window, 300 in flight), on several freshly built and long-running stacks, all kept:

| | 2,000 users (20 runs) | 5,000 users (3 runs) |
|---|---|---|
| Humans who got an entry | **74.0% to 100%** (median 93.6%) | 90.1%, 93.3%, 100% |
| Enter service time p50 | **81 ms to 1,230 ms** | 89, 1,218, 1,448 ms |
| 5xx / timeouts / connection errors | **0 / 0 / 0** (all 23 runs) | |
| People turned away | only ever `WINDOW_CLOSED`: requests still queued when the window closed | |
| Entries told to users = entries in the database | **equal in all 23 runs** | |
| Integrity (I1-I7 SQL) | **pass in all 23 runs** | |
| Scheduler lag p99 | 39 to 78 ms: most runs above the 50 ms bar | |

**The service time is bimodal, and we do not know why.** The same scenario, the same code, the same machine, run back to back,
fell into two modes: a **fast mode** (5 runs: service p50 81 to 89 ms, 99.7 to 100% admitted) and a **slow mode** (15 runs:
service p50 841 to 1,230 ms, 74.0 to 98.5% admitted). The mode flipped between consecutive runs on the *same* stack. We ruled out
by experiment: the API process (a fresh process on the same database stayed slow), the data (a brand-new empty database in the
same Postgres stayed slow), commit latency (`synchronous_commit = off` changed nothing), the port (the same stack was fast then
slow on one port), and the age of the stack (a freshly built stack was fast once and slow later). Not ruled out: CPU contention
and scheduling on this laptop's hybrid-core CPU, background activity, the shared load generator. **Consequence: no capacity or
throughput figure for the engine can be quoted from this machine**, and the 10x spread is itself the finding about our
measurement environment. (An earlier draft of this document quoted "about 155 new entries/s"; that was one mode, and is withdrawn.)

What this says, and what it does not:

- **Said (robust across all 23 runs, in both modes):** every person the system told "entered" has exactly one entry and nobody
  else does; idempotency, window enforcement and "no rank before the draw" held; there were **zero server errors and zero
  timeouts**; the people not admitted were turned away because their requests were queued when the window closed, never because
  of a failure; C's independent I1-I7 checks were clean every time.
- **Not said:** how fast Fair Drop is. Throughput depends on something we could not control by a factor of about ten.
- **Also not said:** anything about fairness. No seats exist in these runs (section 0).

### 3.2 Real engine: kill and restart the API during the flash crowd

A's only API process was hard-killed 3 s after the window opened and restarted 3 s later (`fdsim chaos`; the kill and the
restart are commands fired by the orchestrator, timed by it, and measured from the request timeline). Two runs, on two
different stacks, gave the same outage: 5.6 s and 5.7 s (table: the first run).

| | |
|---|---|
| Kill took effect | 3.01 to 3.27 s |
| Restart began / API serving again | 6.0 s / 8.3 s |
| **Measured outage** (longest stretch with no successful response) | **5.6 s** |
| Requests failed (connection errors) | 600 |
| Humans who got an entry | 1,506 of 2,000; the other 494 were turned away by `WINDOW_CLOSED` |
| **Entries told to users = entries in the database** | **1,506 = 1,506: no acknowledged write was lost** |
| Invariants I1-I7 | pass |
| Control-plane retries needed (close was due when the API was back) | 0 |

This is a single replica, so the restart is a total outage by construction; the interesting result is that **nothing the
system acknowledged was lost and nothing was duplicated**. It says nothing about B's multi-replica claim (B measured 0 failed
requests when killing one of three replicas, on B's stub: that remains B's evidence, not verified here). Two runs are a
demonstration, not a distribution.

Two honest corrections made along the way, both caught by distrusting the output: the first attempt reported "outage 0 s"
because requests were bucketed by when they were *scheduled* (that smeared 200 killed in-flight requests across busy seconds);
the second reported 2 s because silence while clients backed off was not counted. The metric is now defined on completion
time and on the longest no-success stretch (`docs/METRICS.md` 5c), with tests for both mistakes. A third trap: on Windows two
processes can bind the same port, which can mask an outage, so the heal command refuses to start a second replica.

### 3.3 Integration findings (the most useful output so far)

Observed by running A's real code and reading B's. Full table with owners and requests: `docs/INTERFACE_REQUESTS_C.md`
("Integration probe").

1. **A has no draw or claim**, so no run can allocate a seat. FCFS entry is `501` until A's stage 5. (A-P1, A-P2)
2. **A's Windows launcher crashes above about 500 concurrent sockets** with `ValueError: too many file descriptors in
   select()`: it runs on a `SelectSelector` event loop, which Windows caps at 512 descriptors. **B's launcher uses the
   identical loop.** It killed the API during the first real run, and was not documented by either. On Linux it does not apply.
   (A-P3, B-P5)
3. `app.serve` needs Python 3.12; the rest of A's backend runs on 3.11. (A-P4)
4. `GET /events` returns `{events, server_now}`, not the bare array D's schema expects. (A-P5, D-P1)
5. B's package is not integrated, so the defence ablations (E2, E5), detection (E4) and the Sybil-vs-defence claim cannot be
   run on the real stack. B's signals read `defence.identities`, which simulated users do not have, so they would score as
   neutral strangers (B-P4: see section 5).
6. The simulator's CAPTCHA token now matches B's `mint_sim_token` **byte for byte** (golden vectors produced by B's own
   function), and is bound to a user and an event, so a bot cannot mint once and share it across its farm. (B-P2)

### 3.4 What the mock showed (development wiring, **not evidence about A or B**)

The mock implements the agreed rules (one entry per identity, a seeded weighted draw that ignores arrival order, claim holds,
strict-order promotion) plus toy defences. Experiments run against it exercise the whole pipeline. E1 (2 repeats per cell,
1,000 humans, 50 seats, 150 request-flooding bot identities that never solve a challenge):

| Bot request rate vs a person | 1x | 10x | 100x | 1000x |
|---|---|---|---|---|
| Bot seat share, **first-come-first-served** | 0.0% | 10.2% | **100%** | **100%** |
| Bot seat share, Fair Drop, no defences | 14.4% | 15.2% | 12.0% | 14.0% |
| Bots' share of entrants (the lottery-neutral baseline) | 13.1% | 13.1% | 13.1% | 13.1% |

The shape is the one the design predicts: under FCFS volume buys seats; under the lottery it buys nothing and bots land on
their entrant share. Against the toy defences a pure flooder that never solves a challenge gets 0 seats, which says only that
proof-of-work stops a dumb flood, not that defences stop Sybils (those attackers solve every challenge, and are in E2/E5).
The demo-preset numbers are in `simulator/README.md`. **Every mock number carries a MOCK badge in the UI and a MOCK DATA
watermark on the charts.** The 2-repeat intervals are wide on purpose; the planned 10 to 30 repeats were not run against a
target worth the time.

### 3.5 Status of the four claims

| Claim | On the real system | What would settle it |
|---|---|---|
| 1. Bot seat share does not grow with request volume or speed under Fair Drop, but does under FCFS | **Not tested** (no draw; FCFS 501). Mock shows the predicted shape | A's draw, then E1 and E8 |
| 2. Sybil advantage grows with identity count; defences bend the curve | **Not tested** (no draw; no defences wired). Mock only | A's draw + B integrated, then E2, E4, E5 |
| 3. Humans' win probability degrades gracefully | **Not tested** (no seats). Entry success under load *was* measured: 74.0 to 100% at a 10 s window, one replica, depending on an unidentified speed mode | A's draw, then E6 |
| 4. Reliable; zero oversold or duplicate under load | **Partly**: entry path, 23 runs plus two kill-and-restarts, **no acknowledged write lost, no duplicate, zero server errors, I1-I7 clean**. Oversell and claim races cannot be tested without claims | A's claim endpoint, then the concurrency and chaos runs on the multi-replica stack |

## 4. Limitations (mandatory)

- **Single machine.** Generator, API, database share 16 threads; scheduler lag exceeded 50 ms in every real run. Latencies
  include client-side delay and CPU contention. A second machine for the generator is the first fix.
- **Not the shipped stack.** Python 3.11 instead of 3.12, a bundled untuned PostgreSQL, one replica, no nginx, no Redis, no B.
- **Time-compressed windows.** Demo and mock-experiment windows are 6 to 30 s, not minutes; challenge costs were compressed to
  match (PoW 10 bits, human CAPTCHA about 1 s). With uncompressed costs every human is locked out, which would be an artefact.
  Real-target runs must use the real values.
- **Modelled costs.** Device PoW speed, CAPTCHA solve time and failure rate, and every dollar figure are assumptions. PoW hashes
  are computed for real and verified; CAPTCHA is a model (the token format is real). **No real Sybil identity cost exists in
  this simulation**: an attacker with many genuinely verified identities cannot be stopped by software, and we measure only
  how the advantage scales and what the defences cost the attacker in our model.
- **SSE is not measured.** Outcome delivery is modelled with `/status` polling; connection load from push streams is not.
- **Registration is not exercised.** Users are provisioned by SQL; the sign-up flow (OTP, email checks) is untested at scale.
- **Mock defences are toys**; only B's real layers could count as evidence, and they are not integrated.
- **Unstable measurement environment.** Entry speed on this machine flipped between ~85 ms and ~900 ms service time between
  identical runs (section 3.1). Ranges and the full run list are reported instead of means with confidence intervals, because
  the runs are not draws from one distribution.
- **Small n.** 2 chaos runs and 2 repeats per mock cell; the real entry runs are 23 but clustered in two modes.
- **Windows-only measurements.** The 512-socket ceiling and 15.6 ms timer granularity (worked around with
  `timeBeginPeriod(1)`) are Windows effects.
- **Valid for our configuration only** (this population model, these arrival curves, these retry rates).

## 5. Threats to validity

- **Construct.** "Humans" behave as we modelled them (arrival spike, retries, polling, claim delay, no-shows). Real students
  may differ; every parameter is a scenario field and was not tuned to flatter a result.
- **Attacker model drives the identity signals.** B's signals read registration device, IP and timing. When provisioning
  those rows, *we* decide how a bot looks versus a human; that choice encodes our attacker and can make detection look
  better or worse than it is. We have **not** provisioned them (B's schema is not in A's database), and when we do it will be
  stated as an assumption next to every detection number.
- **Latency.** Contaminated by CPU contention and generator lag (above). Use the service time and the shape, not the absolute
  milliseconds, and never quote the mock's latency at all.
- **Population reuse.** The population is fixed across repeats (needed for Jain and Gini over identities), so repeats are
  independent in the draw but not in who the people are.
- **One real kill.** A single outage run does not characterise recovery.
- **Observer effect.** The coordinator polls the database once a second for progress; negligible here, not zero.

## 6. What we would do with more time (in order)

1. **Wait for A's draw and claim**, then run `fdsim doctor` until it is green, then `fdsim suite` against the real stack with
   the real challenge costs and at least 10 repeats at scale: this settles claims 1 to 3.
2. **Put the generator on a second machine** and run behind B's nginx with 3 replicas; re-measure the entry path and the
   50,000-user baseline (E6) with a scheduler lag under 50 ms.
3. **Integrate B**, provision `defence.identities` rows under a stated attacker model, and run E2/E4/E5 for real.
4. **Chaos on the real multi-replica stack** with B's inject commands (`experiments/chaos_kill_replica.yaml`), several
   repetitions per fault, then require invariants.
5. Run the **sign-up flow** for a sample of users to test registration limits; add an SSE client to measure push load.
6. **Find out why entry speed flips 10x on this laptop** (pin processes to cores or disable efficiency mode, watch per-process
   CPU, repeat on a second machine) before quoting any throughput; then profile the entry hot path, size the connection pools, and treat the
   Windows select() ceiling in the launcher docs.

## 7. Index

| What | Where |
|---|---|
| Metric definitions, CI methods, chaos and entry-only rules | `docs/METRICS.md` |
| What C needs from A, B, D, and the integration findings | `docs/INTERFACE_REQUESTS_C.md` |
| Commands, profiles, charts, `/sim` service | `simulator/README.md` |
| Real-target pre-flight | `fdsim doctor` |
| Raw artifacts of every run quoted here | `simulator/results/runs/real_entry_*`, `simulator/results/chaos/` (gitignored; regenerate with the commands above) |
