"""Explainable risk score and the graded response.

score = 1 - prod(1 - c_i),   c_i = weight_i * value_i      (noisy-OR)

* Bounded in [0, 1] and monotone: raising any signal never lowers the score, adding a signal never
  lowers it (property-tested). Every c_i is recorded, so any outcome can be explained to an organiser.
* IP/subnet/ASN signals describe a network, not a person. Their COMBINED contribution is capped at
  `ip_only_cap` (default 0.15: well below the 0.30 challenge threshold, see docs/DEFENCES.md), so a shared campus address can never
  by itself trigger a challenge or a down-weight. Never reject solely on shared IP.
* Response bands (config thresholds, strictly increasing):
      score <  challenge        weight 1.0
      challenge <= score < half weight 1.0  after a challenge (harder PoW / CAPTCHA, if those layers use risk mode)
      half <= score < quarter   weight 0.5  after a challenge
      score >= quarter          weight 0.25 after a challenge
  There is NO score threshold for REJECT: REJECT is reserved for hard evidence (forged tokens).
* Weight output is only ever 1.0, 0.5 or 0.25 (the allocation engine's contract).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config_schema import RiskThresholds, SignalsLayer
from ..contracts import ALLOWED_WEIGHTS
from ..signals.features import IP_ONLY_SIGNALS, Signal


@dataclass(frozen=True)
class Part:
    name: str
    value: float
    weight: float  # configured maximum contribution
    contribution: float  # after the IP-only cap
    detail: dict[str, Any]


@dataclass(frozen=True)
class Risk:
    score: float
    weight: float  # 1.0 | 0.5 | 0.25
    band: str  # low | challenge | half | quarter
    challenge_required: bool
    parts: tuple[Part, ...]

    def to_dict(self) -> dict[str, Any]:
        """What A stores on the entry and what the decision log records. Zero-contribution signals are
        left out to keep it small; `value` and `weight` let a reader recompute everything."""
        return {
            "score": self.score,
            "band": self.band,
            "weight": self.weight,
            "challenge_required": self.challenge_required,
            "signals": [
                {"name": p.name, "value": round(p.value, 4), "weight": p.weight, "contribution": round(p.contribution, 4),
                 **({"detail": p.detail} if p.detail else {})}
                for p in self.parts
                if p.contribution > 0
            ],
        }


def band_and_weight(score: float, t: RiskThresholds) -> tuple[str, float]:
    if score >= t.weight_quarter:
        return "quarter", 0.25
    if score >= t.weight_half:
        return "half", 0.5
    if score >= t.challenge:
        return "challenge", 1.0
    return "low", 1.0


def _noisy_or(values: list[float]) -> float:
    survive = 1.0
    for v in values:
        survive *= 1.0 - v
    return 1.0 - survive


def _cap_scale(contribs: list[float], cap: float) -> float:
    """Largest factor k in [0, 1] such that noisy_or([k*c ...]) <= cap. A noisy-OR is not linear, so
    scaling by cap/group (the obvious thing) overshoots the cap: solve for k by bisection instead."""
    if cap <= 0:
        return 0.0
    if _noisy_or(contribs) <= cap:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(50):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _noisy_or([mid * c for c in contribs]) <= cap else (lo, mid)
    return lo


def combine(signals: list[Signal], cfg: SignalsLayer) -> tuple[float, tuple[Part, ...]]:
    raw = {s.name: cfg.weights.get(s.name, 0.0) * s.value for s in signals}  # type: ignore[call-overload]
    ip_names = [n for n in raw if n in IP_ONLY_SIGNALS]
    scale = _cap_scale([raw[n] for n in ip_names], cfg.ip_only_cap)

    parts: list[Part] = []
    contributions: list[float] = []
    for s in signals:
        c = raw[s.name] * (scale if s.name in IP_ONLY_SIGNALS else 1.0)
        contributions.append(c)
        parts.append(Part(s.name, s.value, cfg.weights.get(s.name, 0.0), c, s.detail))  # type: ignore[call-overload]
    return round(_noisy_or(contributions), 6), tuple(parts)


def assess(signals: list[Signal], cfg: SignalsLayer, thresholds: RiskThresholds) -> Risk:
    score, parts = combine(signals, cfg)
    band, weight = band_and_weight(score, thresholds)
    assert weight in ALLOWED_WEIGHTS and 0.0 <= score <= 1.0
    return Risk(score, weight, band, score >= thresholds.challenge, parts)
