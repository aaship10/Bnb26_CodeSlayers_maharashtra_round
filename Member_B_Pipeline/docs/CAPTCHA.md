# CAPTCHA (stage 4)

One signal, never the whole defence. Code: `backend/app/defence/captcha/`.

## Providers

| provider | what it is | security |
|---|---|---|
| `mock` (default) | accepts the browser demo token and, in simulation mode, SIM_KEY-signed tokens | **none by itself**: it exists so the UI demo works and so Member C can model a CAPTCHA-solving service |
| `turnstile` | Cloudflare Turnstile via its server-side verify API | real, but see "does not stop" |

Chosen real provider: **Cloudflare Turnstile** (free, no image puzzles for most people, privacy-friendlier than
reCAPTCHA). The interface (`CaptchaProvider.verify`) makes hCaptcha or reCAPTCHA a ~40-line adapter if you prefer.

### Mock tokens
* Browser demo: the frontend's mock widget submits `mock-captcha-ok`. Accepted only when `MOCK_CAPTCHA_UI=true`
  (default **only** when `FD_ENV=dev`). Anyone who knows the string passes, so never enable it where it matters.
* Simulator (Member C): `sim1.<user32>.<event32>.<unix_ts>.<mac32>` with
  `mac32 = HMAC-SHA256(SIM_KEY, "sim1|<user32>|<event32>|<unix_ts>")[:32 hex]`, valid 300 s, accepted only while
  `SIMULATION_MODE=true`. `mint_sim_token()` in `providers.py` is the reference. C can attach cost and latency to
  minting this token to model a paid solving service.

### Turnstile (checked against Cloudflare's docs and their live endpoint)
* `POST https://challenges.cloudflare.com/turnstile/v0/siteverify`, form fields `secret`, `response`, optional
  `remoteip`; JSON reply `{success, error-codes, …}`. A token is single use and valid for 300 s, max 2048 chars.
* Configure: `CAPTCHA_SECRET=<secret key>` in `infra/.env` (**never** in event config), and in the event's config
  `captcha: {enabled: true, mode: "always", provider: "turnstile", site_key: "<site key>"}`.
* Dummy keys for testing (from the docs): site keys `1x00000000000000000000AA` (always passes),
  `2x00000000000000000000AB` (always fails), `3x00000000000000000000FF` (forces interactive); secret keys
  `1x0000000000000000000000000000000AA` (passes), `2x0000000000000000000000000000000AA` (fails),
  `3x0000000000000000000000000000000AA` ("token already spent").
  **Observed 2026-10:** the always-pass secret returned `success: true` for *any* token, although the docs page
  implies only the dummy token is accepted. The live-endpoint test (`test_captcha.py`) therefore checks our request
  and response handling, not that bad tokens are rejected.
* **Not done: the browser widget.** The frontend currently renders only the `mock` provider and shows "This check
  isn't available here" for any other. For Turnstile Member D must load Cloudflare's script, render the widget with
  `challenge.captcha.site_key`, and pass the token it produces to `submitCaptcha`. That needs a real site key and
  hostname, so it is not wired or tested here.

## Chaining with proof-of-work

A request carries one `X-Challenge-Id`/`-Solution` pair, so the CAPTCHA challenge is issued *after* a verified PoW
and carries a signed `pow_ok=1` bit in its id (see POW_SPEC). Order: PoW (cheap for us) then CAPTCHA. A good CAPTCHA
never replaces a missing PoW (tested).

## Accessible alternative (the hard rule: no lockout without a fallback)

1. **Proof-of-work is non-visual and automatic**, so it is not a barrier for screen-reader or motor-impaired users.
2. **Organiser waiver** for anyone who cannot use the CAPTCHA (the UI already says "tell the organisers"):
   `POST /admin/defence/waivers {event_id, user_id, reason}` (`X-Admin-Token`), `GET /admin/defence/waivers?event_id=`,
   `DELETE /admin/defence/waivers?event_id=&user_id=`. A waiver skips **only** the CAPTCHA for that person on that
   event; PoW still applies. Stored in Postgres (`defence.waivers`), so it survives restarts and works on every
   replica. Verified in a real browser: the waived person enters with the PoW step only.
3. A provider **outage never locks anyone out and never grants a free pass**: the gate answers `503` with
   `Retry-After: 3` and `details.scope = "captcha"`; the person retries with a fresh challenge when the provider is
   back. (Tested with a failing provider, then a recovered one.)

Open question for you: if the window is short and the provider is down, do you prefer *fail-open with down-weighting*?
That needs the risk engine (stage 5); today the safe default is the retryable 503.

## What it does NOT stop

* CAPTCHA-solving farms (humans paid per solve) and AI solvers: it prices bot entries, it does not identify bots.
  Member C models this with the simulated solving service; treat the measured effect, not the label, as the answer.
* Anyone with many real identities.

**Cost to a legitimate user:** one click (mock) or Turnstile's usually-invisible check, only on events that enable it
(`rate_limit+pow+captcha` for everyone; `all` asks only risky entrants once stage 5 lands).
