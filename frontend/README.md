# Fair Drop frontend

Vite + React 18 + TypeScript (strict), Tailwind v4, TanStack Query, zod.

```bash
npm install
npm run dev:all     # web on :5173 and the mock server on :8787
npm test            # unit, component and mock-contract tests
npm run typecheck
npm run build       # typecheck + production build into dist/
```

Open **http://localhost:5173** (use `localhost`; on Windows Vite binds IPv6 `::1`).

## Running without the other members' services

`npm run mock` starts a contract-faithful stand-in for the backend. Vite proxies the same paths nginx will in production:

| Browser path | Goes to |
|---|---|
| `/api/*` | backend, prefix stripped (the mock for now; override with `API_URL`) |
| `/sim/*` | simulator service (override with `SIM_URL`) |
| `/__mock/*` | mock control endpoints (dev only) |

In dev a floating **Mock** button opens the control panel:

- **Scenarios** jump to a state: before the window, window open, entered, drawing, won with hold, hold expiring, expired, waitlisted, lost, claimed, plus failure setups (429, PoW or CAPTCHA challenge, rejected, flaky network, challenge on claim).
- **Server clock** time-travels to named moments, advances, pauses, or runs 10x or 60x. The mock phase is derived from this clock, so every phase and user state is reachable.
- **Inject failures** per endpoint (`enter`, `claim`, `status`): rate limit, PoW, CAPTCHA, rejected, network failure, slow, 500.
- **Drop SSE streams** to exercise resume.
- Sign-up code for any email is **123456**. The mock CAPTCHA token is `mock-captcha-ok`.
- Proof-of-work difficulty defaults to 18 bits (about a quarter of a second on a desktop). For a more visible "Verifying you're human..." in a demo, start the mock with `MOCK_POW_BITS=22 npm run mock`.

The mock is deterministic: fixed clock origin, fixed events, hash-derived seats, ticket codes and ids. `mock-server/contract.test.ts` validates every mock response against the client's zod schemas, so the mock cannot drift from the client unnoticed.

To point the app at the real backend instead: `API_URL=http://localhost:8000 npm run dev`.

## Layout

```
src/api         client, zod schemas, central error map, endpoints, retry policy
src/lib         server-clock offset, backoff + jitter, safe storage, ids
src/ui          design system (Button, Ticket, Ball, Badge, Alert, Field ...)
src/styles      tokens.css is the single source of truth for the palette
src/features    event, admin, fairness, sim (code-split)
src/dev         styleguide + mock panel (dev builds only, not in dist/)
mock-server     Fastify mock with scenarios, fault injection, SSE with resume
```

Dev-only pages: **/__dev/styleguide** (every component) and **/__dev/pow-bench** (proof-of-work speed on this device).
To test on a phone: `npm run dev:lan`, then open `http://<your-pc-ip>:5173/__dev/pow-bench` (also proves the solver works on plain http, where `crypto.subtle` is missing).
