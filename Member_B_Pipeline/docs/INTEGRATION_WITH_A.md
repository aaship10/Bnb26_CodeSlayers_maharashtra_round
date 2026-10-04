# Integrating B's package into A's real backend

**Status, stated plainly: this integration has NOT been run.** Member A's backend is not in the monorepo yet
(`backend/app/main.py` does not exist; only `backend/app/defence/` does), so everything below was developed and tested against
`infra/stub_api/`, a stand-in written to the contract in `INTERFACE_REQUESTS_B.md` (R1 to R31). The stub is deliberately thin, so
that when A's backend arrives the work is "wire eleven things", not "rewrite". `run.py up --backend real` refuses to start until
`backend/app/main.py` exists and says so (it never silently falls back to the stub).

## The wiring (what A actually writes)
```python
from app.defence import install, lifecycle                     # R5, R17: wraps A's lifespan, adds error handlers and B's routers
from app.defence.gate import entry_gate                         # R1, R3  (the stub reaches it through app/hooks.py)
from app.defence.identity.deps import get_current_user          # R6: the ONE auth dependency
from app.defence.ratelimit.gate import rate_limit               # R19, R23
from app.defence.config import validate_defences                # R7
from app.defence.contracts import Action, GateContext
from app.defence.errors import ApiError, ErrorCode

app = FastAPI(lifespan=a_lifespan)
install(app)

@app.post("/events/{event_id}/enter", dependencies=[Depends(rate_limit("enter"))])
async def enter(event_id, request: Request, user=Depends(get_current_user)):
    ev = await load_event(event_id)
    if existing := await existing_entry(event_id, user["id"]):      # R2: the idempotent fast path comes BEFORE the gate
        return entry_body(existing, already=True)
    check_window(ev)
    decision = await entry_gate(GateContext(user=user, event=ev, request=request, server_now=now(), already_entered=False))
    if decision.action is Action.CHALLENGE:                         # R3
        raise ApiError(ErrorCode.CHALLENGE_REQUIRED, decision.reason or "challenge required", details={"challenge": decision.challenge.to_dict()})
    if decision.action is Action.REJECT:
        raise ApiError(ErrorCode.REJECTED, decision.reason or "rejected", details={"reason": decision.reason})
    insert_entry(event_id, user["id"], weight=decision.weight, risk=decision.risk)   # R25: store both (ON CONFLICT DO NOTHING)
```
`infra/stub_api/app/main.py` is the executable version of exactly this; copy its `/enter` as the starting point.

## Checklist (each item is an R-number in `INTERFACE_REQUESTS_B.md`; "how to check" works against any backend)
| Do | R | How to check |
|---|---|---|
| `install(app)`; one `get_current_user` dependency | R5 R6 R17 | `fd.ps1 check`: API e2e (15) |
| Already-entered fast path before `entry_gate` | R2 | e2e: second `/enter` returns the first entry, no new decision row |
| `CHALLENGE` to 403 `CHALLENGE_REQUIRED`; store `weight` and `risk` on the entry | R3 R25 | D's contract check (20) + browser e2e (24) |
| `PATCH …/config` validates through `validate_defences` | R7 | e2e: unknown key gives `VALIDATION_ERROR` with the path |
| `/readyz` 503 while draining or DB down; SSE ends with `event: reconnect` when draining; **NEW streams refused with 503 while draining** | R8 R29 R32 | `scripts/drain_test.py` (10 checks) |
| Users table shape; `/admin/events/{id}/invariants` in D's `invariantsSchema` shape | R9 R10 | `chaos.py`, D's `npm run smoke` |
| `rate_limit("enter" / "claim" / "status")` on those routes; `/claim` also runs the gate | R19 R22 R23 | `scripts/smoke_attacks.py` |
| uvicorn `proxy_headers=False` (use `infra/local/serve.py` locally) | R15 R16 | e2e: spoofed `X-Forwarded-For` ignored |
| Redis RESP2 if A uses Redis; **psycopg pool with `check=AsyncConnectionPool.check_connection`** | R21 R31 | `chaos.py terminate-pg-connections` (0 failed vs 238 without it) |
| Nothing reads `users.sim_label` | R14 | `test_no_sim_label.py` (AST scan; point it at A's code too) |
| A's worker must not use POSIX-only signal APIs on Windows | n/a | `chaos.py kill-worker` |

## First run when A's code lands
1. Copy A's code into `backend/` (B's `backend/app/defence/` stays as is). If both define `app/hooks.py`, keep A's and call B's `entry_gate` from it.
2. `.\infra\fd.ps1 test` (B's 559 tests; they use the stub, so they pass or fail independent of A).
3. `.\infra\fd.ps1 up -Backend real -Edge nginx -Sim -Mail file`, then `.\infra\fd.ps1 chaos all`, `exec python scripts/drain_test.py`,
   `check`. **Expect first-run failures in the shapes A returns** (field names, status codes): the checks name the field. They are the
   integration test; none of them has been seen to pass against A.
4. `cd ..\frontend; npm run smoke -- --base http://127.0.0.1:8080 --event <id> --admin-token <ADMIN_TOKEN> --writes`.
   Against the stub it is 9 of 15: the six failures are A's (`/fairness`, `/audit`, `/audit/verify`, `/admin/.../stats`) and C's
   (`/sim/*`) routes, which the stub does not have.

## Running D's checklists without Docker
D's `PREFLIGHT_CHECKLIST.md` and `DEMO_SCRIPT.md` say "B's compose" and `docker compose …`. There is no Docker in this project; the
equivalents:

| D's document says | Do this instead |
|---|---|
| `docker compose up -d` | `.\infra\fd.ps1 up -Edge nginx` (once: `.\infra\fd.ps1 get-nginx`; first time also `.\infra\fd.ps1 setup`) |
| `docker compose kill <api-replica>` (demo section 5) | `.\infra\fd.ps1 kill 8002`; to bring it back: `.\infra\fd.ps1 heal` |
| `docker compose down` | `.\infra\fd.ps1 down` |
| `docker compose up --scale api=5` | `.\infra\fd.ps1 scale -N 5` (nginx is re-rendered and reloaded automatically) |
| smoke test `--base http://localhost:8080` | same URL: nginx listens on 8080 (`http://127.0.0.1:8080`) |
| Ops view for the demo | `http://127.0.0.1:8080/ops/` (paste `ADMIN_TOKEN` from `infra/.env`) |

Before a **demo with real people**: simulation mode must be OFF (`up`, not `up -Sim`); the status line of `fd.ps1 status` says
"SIMULATION MODE IS ON" when it is not. Use `up -Sim -Mail file` only for load runs and for Member C's simulator.

## Numbers that differ between documents
* **PoW difficulty**: D's `QA_PREP.md` says the default is 18 bits. The default is `pow.base_bits = 14` (about 9 ms in D's solver on a
  desktop), adaptive up to `max_bits = 24` (risk and load add bits). 18 bits is a preset value, not the default. The doc should say 14
  by default, or the organiser should pick the preset that sets 18. Phone timing is not measured (D to run `/__dev/pow-bench`).
* **Rate limits** are the app's per-identity numbers (Redis); the edge's 400 req/s per IP is only an anti-flood backstop.

## What is real and what is a stand-in
| Real, tested here | Stand-in (A or C owns the real one) |
|---|---|
| everything in `backend/app/defence/` (identity, limits, PoW, CAPTCHA, risk engine, decision log, metrics, drain, breaker), the edge, the runner, chaos/drain/scale/edge tests | `infra/stub_api/` (events, `/enter`, status, SSE, invariants, worker): no seats, holds, claim, lottery, audit, fairness, stats |
| Postgres, Redis and nginx themselves (real processes) | the simulator behind `/sim/*` (C), A's invariant checker (the stub has two checks) |
