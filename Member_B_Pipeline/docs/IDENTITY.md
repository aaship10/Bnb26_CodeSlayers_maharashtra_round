# Identity (stage 2)

Who is allowed in, how they prove it, and what we record about them. Code:
`backend/app/defence/identity/`. Everything here is *verification and evidence*; it never
decides allocation.

## Flow

```
POST /auth/register {email, display_name, hp}   -> 202 {status, expires_in_s, server_now}
        (sends a 6-digit code; registering an EXISTING address sends a login code)
POST /auth/verify   {email, otp}                -> 200 {token, expires_at, user_id, server_now}
POST /auth/refresh  (Authorization: Bearer)     -> 200 {token, expires_at, user_id}
GET  /auth/me       (Authorization: Bearer)     -> 200 {user_id, email, display_name, auth}
POST /admin/sim/tokens {user_ids[], ttl_s}      -> bulk real JWTs (admin + SIMULATION_MODE only; 404 otherwise)
```

Order of checks in `/register`, cheapest first: honeypot → email syntax/canonicalisation →
allowlist + disposable domain → Redis rate limits (ip, /24 subnet, device, email in ONE Lua
call) → Postgres. A request rejected before the limiter never touches Redis or Postgres.

Errors use the shared contract. Validation failures carry a stable machine code:
`details.errors[0].reason ∈ empty, too_long, not_ascii, malformed, bad_domain, bad_local,
empty_after_tag, domain_not_allowed, disposable_domain, otp_must_be_6_digits`. A wrong, expired,
exhausted or unknown code is always the same `401 UNAUTHENTICATED "invalid or expired code"`
so nobody can probe which addresses exist or have pending codes.

## Sessions

* HS256 JWT, claims `sub, iss, iat, exp, auth_time, email, name`. Verified with no database
  lookup, so any replica can authenticate. Algorithm is pinned (`alg: none` and HS512 forgeries
  are rejected, tested).
* Access token 30 min (`JWT_TTL_S`). `POST /auth/refresh` accepts a token that expired up to 6 h
  ago (`JWT_REFRESH_GRACE_S`): a sleeping laptop must not force a new OTP mid-drop. A session can
  never be refreshed past 24 h since the OTP login (`JWT_SESSION_MAX_S`, carried as `auth_time`).
* A 401 from an expired token has `details.reason = "expired"` (refresh), `"invalid"` or
  `"missing"` (log in): the frontend can react correctly.
* **Does not stop:** a stolen token. Stateless tokens cannot be revoked before they expire; the
  30-minute lifetime bounds the damage.

## `get_current_user`

`app.defence.identity.deps.get_current_user` is the single dependency. `AUTH_MODE=dev` trusts
`X-User-Id`; `AUTH_MODE=jwt` requires a bearer token. In jwt mode, `X-User-Id` is honoured only
together with a valid `X-Sim-Key` while `SIMULATION_MODE=true` (used by Member C); otherwise it
is ignored and logged (rate-limited log line). It returns `{id: UUID, email, display_name, via}`
where `via ∈ dev, jwt, sim`; no DB access, so a validly-signed token can name a user the
database lacks: the code that writes rows must handle the foreign-key failure (the stub returns
401).

## Email policy

| step | rule | why |
|---|---|---|
| ASCII only | non-ASCII is rejected, not normalised | silent normalisation is how look-alike duplicates get in |
| canonical form | lower-case, strip `+tag`, collapse dots **only** for gmail/googlemail, fold googlemail→gmail | one mailbox = one identity |
| allowlist | `ALLOWED_EMAIL_DOMAINS` (exact, `*.sub.domain`, or `*`); a bare entry does not admit subdomains | the biggest Sybil brake: attackers need real college mailboxes |
| disposable blocklist | `data/disposable_domains.txt`, subdomains included; applies even with `*` | a **small sample list**: replace with a maintained one |

**Does not stop:** dot tricks at institutional domains (`john.smith` vs `johnsmith` stay distinct,
because collapsing would lock out real students); anyone who owns many real mailboxes.

Soft flags stored on the identity (scored later by the risk engine, never a rejection):

* `sequential_pattern`: ≥ 5 distinct addresses with the same digit-normalised skeleton
  (`studentbot1`, `studentbot2`…) inside 15 minutes. The skeleton needs ≥ 3 letters, so roll-number
  addresses (`2022cs0417`) are never a "pattern".
* `random_local_part`: ≥ 10 chars, entropy ≥ 3.2 bits/char **and** ≥ 3 letter/digit switches
  (`xk2j9qpzv4mw`). Names with a number (`rahul.sharma2021`) stay below the bar.

Measured on the seeded population (48,950 legitimate, 1,050 planted): 0 legitimate addresses
flagged by either rule; 396/400 planted sequential accounts flagged (the first four of a run
cannot be flagged by definition); 150/150 random-looking. These are synthetic numbers from my own
generator, not evidence about real students.

## OTP

6 digits, 10-minute expiry, 5 attempts per code, single use, HMAC-SHA256 with a server pepper
stored in Postgres (never the code). The attempt is charged in the same `UPDATE` that checks the
limits, which also row-locks the pending record: 8 concurrent verifies of one code succeed exactly
once (tested). The honest limit on hashing: a 6-digit code is brute-forceable offline *if* the
pepper leaks; the real protection is online (expiry, attempts, request limits). Guess budget:
each code gives 5 tries, a new code needs `/register` (3 burst + 1 per 5 min per email), so about
15–20 guesses/hour/mailbox out of 10⁶.

**Does not stop:** someone with access to the victim's mailbox; mail-bombing a victim up to the
per-email limit (3 + 1 per 5 min).

## Registration limits (`identity/limits.py`, starting points to tune with C's data)

| scope | register | verify |
|---|---|---|
| ip | 200 burst, 1/s | 600, 10/s |
| /24 subnet | 1000, 5/s | 3000, 50/s |
| device | 5, 1 per 2 min | 20, 1 per 5 s |
| email | 3, 1 per 5 min | 10, 1 per 6 s |

IP and subnet are generous on purpose (orientation day = one campus NAT). `X-Device-Id` is
client-chosen: only well-formed UUIDs are used (garbage is ignored, not trusted), and a client that
rotates device ids evades the device bucket: it is weak evidence, which is why the risk engine
treats it as a signal. If Redis is down, registration and verification switch to the per-process fallback limiter
(the email bucket of 3 is still enforced locally; see `docs/RESILIENCE.md`).

## Honeypot

`hp` is an invisible field. If filled: answer exactly like a success (`202`, same body shape),
create nothing, send nothing. Teaching a bot what failed helps it adapt. **Does not stop:** bots
that skip the field (they would be caught by the other layers, not by this one).

## Data recorded (`defence.identities`)

registration IP + /24, device id, user-agent hash (SHA-256, 16 hex), request time, verification
time, `otp_latency_ms`, email pattern + flags. These feed the stage-5 signals (accounts per
device/IP/subnet, account age, registration velocity, OTP-to-registration latency).
The schema has no foreign key to A's `users` table on purpose.

## Sending real email

With `SMTP_HOST` empty (the dev default) codes are written to `.local/outbox/*.json`. For real delivery
set these in `infra/.env` (never in code or git):

```
SMTP_HOST=smtp.gmail.com   SMTP_PORT=587   SMTP_USER=you@gmail.com
SMTP_PASSWORD=<16-char app password>       MAIL_FROM=you@gmail.com
```
`SMTP_TLS` defaults to `starttls` when `SMTP_USER` is set (port 465 → implicit `tls`). Gmail needs 2-Step
Verification and an **app password**, not the account password; the spaces Google shows in it are stripped.
Safety rules enforced at startup: `SMTP_USER` and `SMTP_PASSWORD` must be set together; a login over an
unencrypted connection to a non-local host is refused; outside `FD_ENV=dev` the file outbox is refused.
The password is never logged or shown in `repr()`.

Sending happens **after** the 202 response (background task), so a mail failure is only visible in the
API logs (`failed to send OTP email`); the user can request another code. Not covered: delivery to spam
folders, provider sending limits (Gmail: roughly 500/day), bounces. Gmail is fine for a demo, not for a
50,000-person drop: use a transactional provider (SES, SendGrid, ...) for that.
Tested with a mocked SMTP call (login/TLS options) and one real send during development.

## Simulation

With `SIMULATION_MODE=true` **and** a valid `X-Sim-Key`: `/auth/register` echoes the OTP in its
response, `X-User-Id` authenticates in jwt mode, `X-Sim-Client-IP` sets the client IP. Without the
key, or with the mode off, all of these are ignored and logged. `SIMULATION_MODE=true` with an
empty `SIM_KEY` refuses to start.
