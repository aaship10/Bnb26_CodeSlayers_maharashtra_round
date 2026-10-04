# Defence config schema (`events.config.defences`)

Source of truth: `backend/app/defence/config_schema.py`. Presets: `presets.py`.
Validation: `config.validate_defences()`. This document describes behaviour, the code
enforces it.

```jsonc
{
  "preset": "all",              // none | rate_limit | rate_limit+pow | rate_limit+pow+captcha | all | custom
  "fail_mode": "local",         // local | open | closed  (what to do if Redis is down)
  "layers": {                   // optional for named presets (see "Presets")
    "rate_limit": { "enabled": true,  ... },
    "pow":        { "enabled": true,  ... },
    "captcha":    { "enabled": true,  ... },
    "signals":    { "enabled": true,  ... },
    "risk":       { "enabled": true,  ... }
  }
}
```

Unknown keys are **errors** (a typo must not silently leave a layer off). New fields will
only ever be added with defaults, so stored configs stay valid.

## Applying it

`PATCH /admin/events/{id}/config` with `{"defences": {...}}` (A's endpoint; header
`X-Admin-Token`). The gate re-reads the config with a 1–2 s cache, so a toggle takes effect
within ~2 s on every replica. `GET /admin/defence/presets` returns every preset with its
full expanded `layers`.

## Presets

| preset | layers enabled | intended use |
|---|---|---|
| `none` | – | baseline: the allocation rule alone |
| `rate_limit` | rate_limit | flood handling only |
| `rate_limit+pow` | rate_limit, pow (`mode: always`) | adds a per-entry cost |
| `rate_limit+pow+captcha` | rate_limit, pow, captcha (`mode: always`) | maximum friction, no risk engine |
| `all` | all five; pow `always`, captcha `risk` | graded response driven by risk score |
| `custom` | whatever you set | hand-tuned experiments |

Rules:

* `{"preset": "all"}` alone expands to the preset's layers.
* A named preset **with** `layers` is accepted only if the layers are identical to the
  preset. Otherwise → `VALIDATION_ERROR` (`type: preset_mismatch`). Overriding silently
  would make experiment labels lie; use `"preset": "custom"` to deviate.
* `layers` without `preset` means `custom`.
* `fail_mode` may be combined with any preset.
* Missing/`null` `defences` = `none`.

## Layers

### `rate_limit`
| field | default | meaning |
|---|---|---|
| `enabled` | false | |
| `limits` | see below | `{endpoint: {dimension: {capacity, refill_per_s}}}` token buckets. Endpoints: `enter claim status challenge`. Dimensions: `identity device ip subnet`. An omitted endpoint/dimension is not limited on that axis. |
| `retry_jitter_max` | 0.25 | `Retry-After` is stretched by a random 0..this fraction (max 0.25) |

Default buckets (**starting points to tune with Member C's data, not measurements**):

| endpoint | identity | device | ip | subnet (/24) |
|---|---|---|---|---|
| enter, claim | 3 burst, 0.3/s | 6, 0.6/s | 300, 50/s | 1500, 250/s |
| status | 2, 0.5/s (≈1 per 2 s) | 4, 1/s | 600, 100/s | 3000, 500/s |
| challenge | 5, 0.5/s | 8, 0.8/s | 300, 50/s | 1500, 250/s |

Per-IP and per-subnet buckets are intentionally ≥20× the per-identity rate: a campus NAT
puts thousands of legitimate students behind one address. Registration limits are
deployment-level (there is no event yet) and live in code/env, not here.

### `pow`
| field | default | meaning |
|---|---|---|
| `mode` | `always` | `always`: every entrant gets a challenge at `base_bits` (+ load bits). `risk`: only entrants with score ≥ `risk.thresholds.challenge`. **Until the risk engine exists (stage 5), `risk` mode challenges nobody.** |
| `base_bits` | 14 | leading zero bits required. Cost doubles per bit. Measured on a desktop with D's solver: 14 bits ≈ 9 ms, 20 bits ≈ 0.4 s, 22 bits ≈ 1.1 s (`docs/POW_SPEC.md` §6); **phone not measured**. |
| `max_bits` | 24 | hard cap after adaptive extras |
| `risk_bits_max` | 6 | extra bits at risk score 1.0 |
| `load_bits_max` | 2 | extra bits at full load |
| `load_ref_rps` | 200 | gate calls per second (per event, whole cluster, last complete second) that count as full load |
| `ttl_s` | 60 | challenge lifetime |
| `single_use` | true | best-effort Redis SETNX; replay is harmless anyway (bound to user + event, entry is idempotent) |

### `captcha`
| field | default | meaning |
|---|---|---|
| `mode` | `risk` | `always` or `risk` (same meaning as pow) |
| `provider` | `mock` | `mock` or `turnstile`. The real adapter is chosen/verified in stage 4 against the provider's docs. |
| `site_key` | `""` | public key sent to clients. **The secret key is env-only**, never stored in event config. Required for non-mock providers. |

### `signals`
| field | default | meaning |
|---|---|---|
| `weights` | see code | per-signal **maximum contribution** to the score, each in [0,1] |
| `ip_only_cap` | 0.15 | total ceiling for the network signals together (IP, /24, ASN, registration velocity): shared NAT ⇒ soft evidence. Measured: 0.25 left too little headroom and challenged 0.32% of legitimate students; 0.15 challenged none (`docs/DEFENCES.md`) |
| `timing_min_samples` | 5 | requests needed before timing regularity is scored |

Signals: `timing_regularity header_anomaly accounts_per_device accounts_per_ip
accounts_per_subnet accounts_per_asn account_age registration_velocity email_pattern
email_entropy otp_latency`. The honeypot is **not** a weighted signal: a hit is hard
evidence and leads to REJECT.

### `risk`
`thresholds`: `challenge` 0.30 < `weight_half` 0.55 < `weight_quarter` 0.75 (strictly
increasing, enforced). Score below `challenge` → ALLOW at weight 1.0. There is deliberately
**no reject threshold**: REJECT is reserved for hard evidence (honeypot, forged/invalid
tokens). Output weights are only 1.0, 0.5, 0.25.

Score combination (implemented in stage 5): noisy-OR,
`score = 1 − Π(1 − cᵢ)` with `cᵢ = wᵢ·sᵢ` and each signal value `sᵢ ∈ [0,1]`. It is bounded in [0,1], and
raising or adding a signal never lowers it (property-tested). The four **network** signals (`accounts_per_ip`,
`accounts_per_subnet`, `accounts_per_asn`, `registration_velocity`) are scaled down together so that their combined
noisy-OR never exceeds `ip_only_cap`; the scaling factor is solved exactly (noisy-OR is not linear).

Bands: `score < challenge` weight 1.0 · `challenge ≤ score < weight_half` weight 1.0 after a challenge ·
`weight_half ≤ score < weight_quarter` weight 0.5 after a challenge · `score ≥ weight_quarter` weight 0.25 after a
challenge. "After a challenge" means: if `pow`/`captcha` are in `mode: "risk"`, entrants at or above `challenge` must
solve them (PoW difficulty also rises by `round(risk_bits_max × score)`); layers in `mode: "always"` challenge everyone
regardless. With no challenge layer enabled the band still sets the weight.

What is stored with every entry (`risk` JSON, also in the decision log): `{score, band, weight, challenge_required,
signals: [{name, value, weight, contribution, detail?}], challenges_passed?}`; zero-contribution signals are omitted.
Missing evidence is neutral: a person with no `defence.identities` row gets no identity-derived penalty.

## Cross-layer rules (enforced)

* `risk.enabled` ⇒ `signals.enabled`.
* `pow.mode = risk` ⇒ `risk.enabled`; likewise `captcha.mode = risk`.
* `pow.base_bits ≤ pow.max_bits`.
* Non-mock CAPTCHA provider ⇒ non-empty `site_key`.

With the risk engine off, `pow`/`captcha` in `mode: always` challenge everyone: that is what
the `rate_limit+pow(+captcha)` presets measure.

## `fail_mode` (Redis outage)

| value | rate limiting | PoW single-use | trade-off |
|---|---|---|---|
| `local` (default) | per-process best-effort limiter | skipped | weaker with several replicas (limits are per process), never blocks everyone |
| `open` | none | skipped | maximum availability, no flood protection |
| `closed` | 503 on limited endpoints | skipped | protects the DB, sacrifices availability |

Allocation integrity never depends on Redis in any mode (A's database enforces it).
