"""CAPTCHA cost model (there is no real CAPTCHA to solve against the mock, and B's
provider isn't wired yet). A CAPTCHA "solve" is modelled as: wait a solve time, then
with probability (1 - fail_rate) return the mock provider token, else fail.

For humans the solve time models a person squinting at images; for bots it models a
paid solving service (per-solve price in AttackerCost.per_captcha, latency per solve).
This is explicitly a MODEL, surfaced as a limitation in FINDINGS.md: we never pay a
real solving service.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

MOCK_CAPTCHA_TOKEN = "mock-captcha-ok"  # same token the mock (and D's mock) accept


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
