"""CAPTCHA cost model (there is no real CAPTCHA to solve against the mock, and B's
provider isn't wired yet). A CAPTCHA "solve" is modelled as: wait a solve time, then
with probability (1 - fail_rate) return the mock provider token, else fail.

For humans the solve time models a person squinting at images; for bots it models a
paid solving service (per-solve price in AttackerCost.per_captcha, latency per solve).
This is explicitly a MODEL, surfaced as a limitation in FINDINGS.md: we never pay a
real solving service.
"""
from __future__ import annotations

import hashlib
import hmac
import random
import time
import uuid
from dataclasses import dataclass

MOCK_CAPTCHA_TOKEN = "mock-captcha-ok"  # the fixed demo token D's UI and the dev mock accept
SIM_TOKEN_TTL_S = 300  # B's providers.py SIM_TOKEN_TTL_S


def hex32(value: str) -> str:
    """32 hex chars for a user or event id: a UUID's .hex when it is one (A's and B's ids are), else a
    stable hash (the dev mock uses readable event ids). Both ends use this, so they agree."""
    try:
        return uuid.UUID(str(value)).hex
    except ValueError:
        return hashlib.sha256(str(value).encode()).hexdigest()[:32]


def mint_sim_token(sim_key: str, user_id: str, event_id: str, ts: int | None = None) -> str:
    """Member B's simulator CAPTCHA token (backend/app/defence/captcha/providers.py mint_sim_token):
        sim1.<user32>.<event32>.<unix_ts>.<mac32>,  mac32 = HMAC-SHA256(SIM_KEY, "sim1|<user32>|<event32>|<ts>")[:32]
    Valid 300 s, accepted only while the server runs SIMULATION_MODE=true. Verified against B's own
    function with golden vectors (tests/test_captcha_token.py). This models a paid solving service:
    the cost and latency are the model's (CaptchaModel), the token is the real thing."""
    ts = int(time.time()) if ts is None else ts
    u, e = hex32(user_id), hex32(event_id)
    mac = hmac.new(sim_key.encode(), f"sim1|{u}|{e}|{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"sim1.{u}.{e}.{ts}.{mac}"


def verify_sim_token(token: str, sim_key: str, user_id: str, event_id: str, now: float | None = None) -> bool:
    """What B's MockCaptcha does with a sim token; used by the dev mock for parity."""
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != "sim1" or not parts[3].isdigit():
        return False
    if parts[1] != hex32(user_id) or parts[2] != hex32(event_id):
        return False
    now = time.time() if now is None else now
    if abs(now - int(parts[3])) > SIM_TOKEN_TTL_S:
        return False
    want = hmac.new(sim_key.encode(), f"sim1|{parts[1]}|{parts[2]}|{parts[3]}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(want, parts[4])


@dataclass(frozen=True)
class CaptchaModel:
    solve_s_mean: float = 8.0
    fail_rate: float = 0.05
    token: str = MOCK_CAPTCHA_TOKEN

    def solve_time(self, rnd: random.Random) -> float:
        """Seconds to produce a token. Exponential around the mean, floored so it's never 0."""
        return max(0.05, rnd.expovariate(1.0 / self.solve_s_mean)) if self.solve_s_mean > 0 else 0.0

    def succeeds(self, rnd: random.Random) -> bool:
        return rnd.random() >= self.fail_rate
