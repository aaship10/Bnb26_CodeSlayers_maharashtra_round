# Defence decision log

Why each layer exists, what it does NOT stop, what it costs an honest person, its knobs, and the evidence so far.
Evidence has three grades, and I say which one applies:

* **measured, synthetic**: produced by my own seeder/scripts. Shows the machinery behaves sensibly; says nothing about
  real attackers (the bots are as clumsy or clever as I made them).
* **tested**: a unit/integration/browser test asserts the behaviour.
* **pending C**: needs Member C's simulator runs (real attack conditions, allocation outcomes). The deciding evidence for
  every layer is *how the allocation changes under attack with the layer on vs off*. Where it is missing, the layer's
  value is unproven, and if C's runs show no effect the layer should be cut.

The allocation rule (A's time-window lottery) already neutralises speed, volume and repeats. These layers exist for the
remaining problem: **many accounts** (Sybil) and **load**.

| layer | purpose | evidence today |
|---|---|---|
| email policy + OTP (stage 2) | one mailbox = one identity; real mailboxes cost something | tested; 0 of 48,950 synthetic legitimate addresses flagged |
| honeypot | cheap catch of form-filling bots | tested (looks like success, creates nothing) |
| registration limits | slow identity minting per device/email/IP | tested; Sybil from one device capped at 5 (smoke) |
| edge limiter + microcache (stage 3) | survive floods and stampedes | tested + smoke; stampede 30 → 1 upstream request |
| per-endpoint Redis limits | fairness per identity/device/IP | tested + smoke; spoofed headers earn no budget |
| proof-of-work (stage 4) | per-entry cost, raisable under risk/load | tested + real browser; **default cost to a bot is tiny: pending C** |
| CAPTCHA (stage 4) | human check for risky entrants | tested (mock), Turnstile adapter vs live endpoint; widget not wired |
| signals + risk engine (stage 5) | graded response to Sybil evidence | measured, synthetic (below); **pending C** |
| decision log (stage 5) | lets C compute precision/recall; audit trail | tested |

---

## Identity: email policy, OTP, honeypot, registration limits
**Why.** Sybil attacks need identities; each real mailbox on the allowed domain is a cost to the attacker.
**Stops.** Disposable and off-domain addresses, `+tag` aliases and Gmail dot aliases (one identity), sequential
`user1@, user2@…` address runs, instant form-filling bots, mass registration from one device or email.
**Does NOT stop.** Anyone who owns many real mailboxes; dot tricks at institutional domains (`john.smith` vs
`johnsmith` stay distinct because collapsing would lock out real students); a person who owns the victim's inbox.
**Cost to an honest person.** One code, once. Rare false refusals: a student whose college domain is not on the allowlist.
**Knobs.** `ALLOWED_EMAIL_DOMAINS`, `identity/limits.py` buckets, `OTP` constants, disposable list file.
Full detail and numbers: `docs/IDENTITY.md`.

## Rate limiting and edge
**Why.** Keep the system up and fair during a flash crowd; shed cheaply (no Postgres on the shed path).
**Stops.** One identity/device/IP hammering an endpoint; a stampede on the event page.
**Does NOT stop.** A distributed attacker who stays under every per-identity limit (that is the risk engine's and the
allocation rule's job). Redis outage: a per-process fallback limiter takes over (`fail_mode: local`, the default);
`open` and `closed` are available; a circuit breaker keeps the outage from slowing every request (`docs/RESILIENCE.md`).
**Cost.** None for normal use; a shared campus address is protected by deliberately generous IP/subnet buckets.
**Knobs.** `layers.rate_limit.limits`, `fail_mode`, `EDGE_BURST/EDGE_RPS`. Detail: `docs/RATE_LIMITING.md`.

## Proof-of-work
**Why.** A per-entry computational cost that can be raised for risky or heavily loaded moments.
**Stops.** Unsolved requests, replay across people, forged/easier challenges (stateless HMAC token, tested).
**Does NOT stop.** A bot that simply solves it: at the default 14 bits a native solver needs about a millisecond, so on
its own it is **nearly free for an attacker with CPUs**. Whether it changes allocation under attack is **pending C**.
**Cost.** About 9 ms on a desktop at 14 bits (D's solver, measured), ~0.4 s at 20 bits, 1.1 s at 22. **Phone not measured.**
**Knobs.** `layers.pow.{base_bits,max_bits,risk_bits_max,load_bits_max,load_ref_rps,ttl_s,single_use,mode}`. Spec: `docs/POW_SPEC.md`.

## CAPTCHA
**Why.** One extra signal for entrants the risk engine distrusts.
**Stops.** Plain scripts. **Does NOT stop.** Solving farms, AI solvers, many real identities.
**Cost.** One click or Turnstile's usually invisible check, only when the preset/risk asks for it.
**Fallback (hard rule).** PoW needs no interaction; organisers can waive the CAPTCHA per person; a provider outage is a
retryable 503, never a lockout and never a free pass. Detail: `docs/CAPTCHA.md`.

---

## Signals and the risk engine
**Why.** The allocation rule cannot tell one person from 200 accounts; this layer gives bot-like accounts a lower
chance (weight 0.5 or 0.25) and asks suspicious entrants for more proof, without ever rejecting on a score.

Score = noisy-OR of `weight × value` over the signals (bounded in [0, 1], monotone: property-tested; every contribution
is stored with the entry so any outcome can be explained). Bands: below 0.30 weight 1.0; 0.30–0.55 challenge, weight 1.0;
0.55–0.75 challenge, weight 0.5; 0.75+ challenge, weight 0.25. **REJECT only for hard evidence** (forged tokens).

| signal | what it measures | max contribution | false-positive behaviour |
|---|---|---|---|
| accounts_per_device | accounts sharing the registration device id | 0.45 | siblings/lab PCs: 3 accounts = 0.09; 11+ saturates. **Computer-lab machines are a real risk** |
| otp_latency | seconds between asking for and entering the code | 0.15 | autofill from SMS can be fast (3–4 s ≈ 0.1) |
| account_age | minutes since the account was verified | 0.15 | late registrants are legitimate; hence small |
| email_pattern / email_entropy | flags set at registration | 0.30 / 0.10 | 0 of 48,950 synthetic legitimate addresses flagged |
| header_anomaly | missing/automation User-Agent, headless, no Accept-Language | 0.20 | trivially faked; only catches lazy bots. Real headless Chrome scores 0.6 |
| timing_regularity | regularity (CV) of one identity's /enter and /challenge gaps | 0.35 | **not** applied to /status polling; ±25 % jitter still scores ~0.5 by design |
| accounts_per_ip / subnet / asn, registration_velocity | **network evidence** | **capped together at 0.15** | see next paragraph |

**Network evidence is capped, on purpose.** Thousands of real students share a campus NAT address, and on orientation day a
whole hostel registers within minutes (identical to a bot burst from one subnet). The four network signals are therefore
combined and capped (`ip_only_cap`, default **0.15**), well below the 0.30 challenge threshold: network evidence alone can
never challenge or down-weight anyone (tested with 1,200 accounts on one address).

### Measured on the seeded population (synthetic): `python scripts/measure_risk.py`
48,950 legitimate students (35 % behind 12 campus-NAT addresses, 16,991 of them), 1,050 planted bots; thresholds above.
Model A = careful bots with ordinary browser headers (best case for the attacker); model B = bots with a script's
default User-Agent. Timing regularity needs live logs and is not modelled.

| cluster (n) | A: challenged | A: weight ≤ 0.5 | B: challenged | B: weight ≤ 0.5 |
|---|---|---|---|---|
| legitimate (48,950) | **0.0 %** | 0 % | **0.0 %** | 0 % |
| legitimate behind campus NAT (16,991) | 0.0 % | 0 % | 0.0 % | 0 % |
| sequential emails (400) | 99.0 % | 0 % | 100 % | 99.0 % |
| 200 accounts, one device | 100 % | 0 % | 100 % | 100 % |
| 300-account burst, one IP | 100 % | 0 % | 100 % | 0 % |
| random-looking addresses (150) | **2.7 %** | 0 % | 100 % | 0 % |

Detection at the challenge threshold: model A recall 85.7 %, precision 100 %; model B recall 100 %, precision 100 %;
legitimate false-positive rate 0 of 48,950.

**What this honestly shows, and does not.**
* No bot cluster reaches weight 0.25 and only the scripted device farm and sequential run are down-weighted. By default a
  careful bot is *challenged* (PoW, then CAPTCHA) but, once it solves, **keeps weight 1.0**: the allocation effect rests on
  how much the challenges cost it. That is exactly what Member C must measure.
* Careful bots with name-like random addresses are mostly missed (2.7 %); the 300-account burst is caught only through
  fast OTP entry and fresh accounts. A burst of careful bots with human-like OTP timing from a shared address would pass:
  it is indistinguishable from orientation day by network evidence alone.
* To down-weight device farms, raise `signals.weights.accounts_per_device` to 0.6: the 200-account device cluster then
  lands on weight 0.5 (100 %) with legitimate false positives still 0 (measured), at the price of penalising up to
  ~11 people sharing one browser profile (a lab PC). That is a policy choice for the organiser, so I did not make it default.
* The seeded data is mine: a real population has shapes I did not generate.

### Decisions taken from measurement (so they are not mysterious later)
1. **`ip_only_cap` 0.25 → 0.15.** At 0.25 all 158 false positives (0.32 % of legitimate students) were campus-NAT students
   whose network evidence already sat at the cap, so a quick OTP entry or a shared device tipped them over 0.30.
   At 0.15: 0 false positives, recall 86.4 % → 85.7 %.
2. **`registration_velocity` moved into the capped network group.** A same-subnet burst cannot be told from a legitimate
   mass registration; it must not challenge anyone by itself. (Found by a failing test, then confirmed above.)
3. **Noisy-OR cap solved exactly (bisection).** Scaling by cap/group overshoots the cap because noisy-OR is not linear;
   a test caught it (0.2559 vs cap 0.25).

## Decision log (`defence.decisions`) and the API for C
Every gate decision, including `CHALLENGE` and `REJECT` (which create no entry), is recorded: event, user, time, action,
weight, score, the full signal breakdown, layer, IP, device, reason. `GET /admin/defence/decisions?event_id=&cursor=&limit=&action=`
(keyset pagination, oldest first) and `/decisions/summary`. **No ground-truth column exists**; C joins `user_id` with their own
labels. It is written asynchronously in batches: **a crash can lose the last unflushed batch (≤ 0.5 s), by design**
(analytics, not state). The queue is bounded: when full, new records are dropped and counted (`log.dropped` in the summary)
rather than slowing requests; a failing database drops a batch after one retry (`failed_batches`). A read flushes the
serving replica's queue; other replicas lag by at most 0.5 s.

## Failure policy (what happens when parts break)
| failure | behaviour |
|---|---|
| Redis down | rate limiting falls back to the local per-process limiter (`fail_mode: local`; `open` = none, `closed` = 503); load = 0; timing signal empty; single-use skipped; cluster counts come from Postgres; a circuit breaker skips Redis instantly |
| Postgres down | gate returns an error for new entries (A's allocation cannot proceed anyway); limiter keeps working; decision log drops batches |
| signal computation raises | entry proceeds without a score and the error is logged at ERROR (never silent) |
| CAPTCHA provider down | retryable 503 `Retry-After: 3`, never a lockout, never a free pass |
| stored config invalid | last good config is kept, never "no defences" |

## Open items
* Phone cost of PoW is unmeasured. * Turnstile widget is D's to wire. * `/claim` is not gated or limited yet (A's route; R22/R23).
* Abrupt-fault recovery times (stage 7 chaos). * Everything marked "pending C".
