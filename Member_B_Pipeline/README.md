# Fair Drop: Member B (Defence & Infra)

Everything Member B owns, laid out as the shared monorepo so it can be merged as-is.
**No Docker**: everything runs as local processes.

```
Member_B_Pipeline/
├── backend/
│   ├── app/defence/            # B's package (A wires it via app/hooks.py + install(app))
│   │   ├── contracts.py        # GateContext / GateDecision / Challenge (the contract with A)
│   │   ├── config_schema.py presets.py config.py     # events.config.defences
│   │   ├── identity/           # register, OTP, JWT, get_current_user, email policy      (stage 2)
│   │   ├── ratelimit/          # Redis Lua token bucket + 429 contract                   (stage 2/3)
│   │   ├── clientip.py simheaders.py runtime.py migrate.py migrations/ data/
│   │   ├── pow/ captcha/       # stage 4: signed challenges, adaptive difficulty, Turnstile + mock, waivers
│   │   ├── signals/ risk/      # stage 5: signal functions, noisy-OR risk engine, cluster counts (Postgres truth, Redis cache)
│   │   ├── decisionlog/        # stage 5: async batched decision log
│   │   └── metrics.py resilience.py lifecycle.py     # stage 6: Prometheus metrics, circuit breaker, graceful drain
│   └── tests_defence/          # B's tests (A keeps backend/tests)
├── infra/
│   ├── local/                  # run.py (stack runner), pg.py, gateway.py (+ ops/ dashboard), serve.py, fake_redis.py
│   ├── stub_api/               # stand-in for A's backend (same hook signature)
│   ├── nginx/                  # nginx edge templates (D's config merged with B's rules), rendered by run.py; get_nginx.py fetches the binary
│   ├── chaos/                  # chaos.py: six faults under load, results/*.json
│   ├── .env.example  fd.ps1
├── scripts/                    # seed_identities, local_e2e, frontend_contract_check, browser_e2e, smoke_attacks, pow_solver.{py,mjs}, pow_bench.ts, measure_risk.py, drain_test.py, scale_test.py, edge_test.py
├── docs/                       # CONFIG_SCHEMA, IDENTITY, RATE_LIMITING, POW_SPEC, CAPTCHA, DEFENCES (decision log + evidence), OBSERVABILITY, RESILIENCE, EDGE, CHAOS, INTEGRATION_WITH_A, INTERFACE_REQUESTS_B
└── .local/                     # runtime state: private Postgres data, logs, OTP outbox (git-ignored)
```

## First run (Windows / PowerShell)

Needs Python >= 3.12 and an installed PostgreSQL (only its binaries are used; your own server is
never touched). For the frontend checks also Node 22 and Chrome/Edge (no browser download).

```powershell
.\infra\fd.ps1 setup                  # creates .venv, installs dependencies
.\infra\fd.ps1 get-redis              # optional but recommended: real Redis (Windows build) into .local/redis
(cd ..\frontend; npm install; npm run build)    # the gateway serves frontend/dist when it exists
.\infra\fd.ps1 up                     # 3 API replicas + gateway; open http://127.0.0.1:8080
.\infra\fd.ps1 check                  # restarts the stack with the FILE mail outbox, then runs 4 suites (below)
.\infra\fd.ps1 resilience             # live: graceful drain with traffic flowing + a 1,500-user flash crowd with consistency checks
.\infra\fd.ps1 up -Sim -Mail file     # simulation mode (X-Sim-Key, token minting): only for load tools / Member C, never a real event
# ops dashboard (nothing to install): http://127.0.0.1:8080/ops/  (paste ADMIN_TOKEN from infra/.env)
.\infra\fd.ps1 test                   # unit + integration tests (starts Postgres itself)
.\infra\fd.ps1 up -Mail file           # never send real email (codes go to .local/outbox); plain `up` uses SMTP if configured
.\infra\fd.ps1 exec python scripts/seed_identities.py --reset      # 50,000 dummy identities (--reset WIPES the dev DB's users!)
python scripts/measure_risk.py        # what the risk engine does to the seeded population (in memory, touches nothing)
.\infra\fd.ps1 status ; .\infra\fd.ps1 logs ; .\infra\fd.ps1 scale -N 5 ; .\infra\fd.ps1 down
```
`python infra/local/run.py <up|down|status|logs|scale|kill|heal|exec>` is the same without PowerShell.

```powershell
.\infra\fd.ps1 get-nginx ; .\infra\fd.ps1 up -Edge nginx     # nginx as the edge (docs/EDGE.md); the dashboard stays on /ops/
.\infra\fd.ps1 edge-check                                      # microcache, limiter, public surface and drain, through nginx
.\infra\fd.ps1 up -Sim -Mail file ; .\infra\fd.ps1 chaos all  # six faults under load (docs/CHAOS.md); then `up` for normal mode
.\infra\fd.ps1 kill 8002 ; .\infra\fd.ps1 heal               # the demo's "kill a replica", without Docker
```

`check` runs, against the live stack through the gateway (one origin, like production):
1. `scripts/local_e2e.py`: API flow, spoofed-header handling, alias collapse, 429 contract (15 checks)
2. `scripts/frontend_contract_check.ts`: real responses parsed by **D's own zod schemas** (20 checks)
3. `scripts/browser_e2e.mjs`: D's built UI driven in real Chrome: sign up, reload, enter, alias on another device,
   PoW solved by D's worker, PoW + CAPTCHA click, an organiser waiver, a timed 20-bit puzzle (18 checks;
   screenshots in `.local/screens/`)
4. `scripts/smoke_attacks.py`: flood, spoofed headers, honeypot, one-device Sybil, PoW bypass attempts and
   solution replay, each with the layer off then on

What `up` does: creates `infra/.env` with fresh random secrets, initialises a **private** Postgres cluster
in `.local/pg` (port 5433), starts Redis (your `REDIS_URL`, else `.local/redis` from `get-redis`, else a
fakeredis server: fine functionally, too slow to flood-test: rate limiting then fails open and logs it),
starts the replicas through `infra/local/serve.py` (selector event loop, required by psycopg on Windows),
and the gateway. OTP emails go to `.local/outbox/*.json` unless `SMTP_HOST` is set.

## Stage status

1. Skeleton, stub API, config schema + presets: **done**
2. Identity (register/OTP/verify/JWT, email policy, honeypot, registration limits, seeder): **done**
3. Rate limiting + edge (per-endpoint Redis limits, microcache, smoke attacks) and frontend integration: **done**
4. PoW + CAPTCHA (signed stateless challenges, adaptive difficulty, Turnstile + mock, organiser waivers): **done**
5. Signals, risk engine, weights 1.0/0.5/0.25, decision log + API, measured on the seeded population: **done**
6. Observability and resilience (metrics, ops dashboard, Redis circuit breaker + local fallback limiter, graceful drain,
   SSE stream in the stub, scale + drain tests): **done**. Not done: PgBouncer (documented why), Prometheus/Grafana verified on real servers
7. Chaos scripts, nginx edge, final docs, integration readiness: **done, except the integration itself**. `docs/CHAOS.md`: six faults,
   invariants hold in all; the one visible outage is the edge itself. **Not done: running against Member A's real backend** (it is not in
   the monorepo; `docs/INTEGRATION_WITH_A.md` has the wiring, the checklist and what to expect to fail first) and against Member C's
   simulator. Everything was tested against the stub backend.
