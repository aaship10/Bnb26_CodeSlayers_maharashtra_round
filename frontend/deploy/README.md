# Deploying the frontend (for Member B)

The frontend is a static build served by B's nginx on `:8080`, next to `/api` (A) and `/sim` (C). Everything in this folder is a proposal for `infra/`. Copy it, rename the upstreams, and own it.

## What to ship

```bash
cd frontend
npm ci
npm run build            # typecheck + production build into frontend/dist
```

`dist/` is self-contained. Asset file names are content-hashed, so they can be cached forever; `index.html` must never be cached.

## nginx

Three files in `deploy/nginx/`:

| File | Purpose |
|---|---|
| `fairdrop-frontend.conf` | The `server { listen 8080; ... }` block: SPA fallback, `/api` (prefix stripped) and `/sim` (prefix kept), SSE locations with buffering off and 1 h timeouts, immutable caching for `/assets/`, `no-cache` for the app shell, gzip, and `/__mock` blocked. |
| `fd-security-headers.conf` | CSP and other security headers. It's included in every location that sets its own `add_header`, because nginx drops inherited headers otherwise. |
| `fd-proxy.conf` | Shared proxy settings (HTTP/1.1 keep-alive, forwarded headers). |

Change the two upstreams (`api:8000`, `simulator:8100`) to the compose service names and replica list.

### docker-compose sketch

```yaml
  web:
    image: nginx:1.28-alpine
    ports: ["8080:8080"]
    volumes:
      - ./frontend/dist:/usr/share/nginx/html:ro
      - ./frontend/deploy/nginx/fairdrop-frontend.conf:/etc/nginx/conf.d/default.conf:ro
      - ./frontend/deploy/nginx/fd-security-headers.conf:/etc/nginx/fd-security-headers.conf:ro
      - ./frontend/deploy/nginx/fd-proxy.conf:/etc/nginx/fd-proxy.conf:ro
    depends_on: [api, simulator]
```

(Includes resolve relative to `/etc/nginx/`, which is why the two snippets sit there.)

## How this was verified

On nginx 1.28 (Windows build), with this exact config pointed at the mock server:

- `nginx -t` passes. It also caught a duplicate `proxy_read_timeout`, now fixed.
- The **full Playwright suite (29 tests) passes through `:8080`**, including SSE live updates, `Last-Event-ID` resume after a dropped stream, and the polling fallback.
- A CSP test (`e2e/csp.spec.ts`) runs the main screens, the draw-verifier worker and the charts under the production CSP and finds **zero violations**. It previously caught Vite inlining tiny font files as `data:` URIs; the build now never inlines fonts.
- `npm run smoke -- --base http://localhost:8080 ...` passes 20/20.

## Checking a real deployment

```bash
# 1. contract: every endpoint the UI uses, validated with the app's own schemas
npm run smoke -- --base http://localhost:8080 --event <event_id> --dev-user <uuid> --admin-token $ADMIN_TOKEN
#    add --writes to also enter the event and start a (mock-target) simulator run

# 2. the browser flows (needs the mock's control port for scenarios, so run this against a mock-backed stack)
E2E_BASE_URL=http://localhost:8080 npm run e2e:quick
```

A smoke failure prints the endpoint and the first schema mismatches (field path and reason). That's the list to resolve with A, B or C.

## Open points for B

- **B8:** if a hosted CAPTCHA is used, its script and frame origins must be added to the CSP.
- **SSE:** keep the two `.../stream` locations above the generic `/api/` and `/sim/` ones (nginx checks regex locations first, but keep them visible).
- **`/sim` prefix:** the config assumes C's service expects paths that start with `/sim/`. If it doesn't, change `proxy_pass http://fd_sim;` to `proxy_pass http://fd_sim/;` inside `location /sim/`.
