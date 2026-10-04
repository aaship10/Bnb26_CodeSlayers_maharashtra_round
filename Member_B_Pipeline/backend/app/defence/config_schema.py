"""Pydantic models for events.config.defences (documented in docs/CONFIG_SCHEMA.md).

Rules of the schema:
* extra="forbid" everywhere: a typo like "enabeld" must fail loudly mid-demo
  instead of silently leaving a layer off.
* New fields must ship with defaults so config blobs stored earlier stay valid.
* Only public values live here. Secrets (CAPTCHA secret key, SIM_KEY, JWT secret)
  come from the environment, never from event config, because A's admin API
  stores and echoes this blob.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Dimension = Literal["ip", "subnet", "identity", "device"]
Endpoint = Literal["enter", "claim", "status", "challenge"]
FailMode = Literal["local", "open", "closed"]
PresetName = Literal["none", "rate_limit", "rate_limit+pow", "rate_limit+pow+captcha", "all", "custom"]
ChallengeMode = Literal["always", "risk"]
SignalName = Literal[
    "timing_regularity",
    "header_anomaly",
    "accounts_per_device",
    "accounts_per_ip",
    "accounts_per_subnet",
    "accounts_per_asn",
    "account_age",
    "registration_velocity",
    "email_pattern",
    "email_entropy",
    "otp_latency",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------- rate limit
class Bucket(_Strict):
    """Token bucket: `capacity` is the burst size, `refill_per_s` the sustained rate."""

    capacity: float = Field(gt=0, le=1_000_000)
    refill_per_s: float = Field(gt=0, le=1_000_000)


def _b(capacity: float, refill_per_s: float) -> Bucket:
    return Bucket(capacity=capacity, refill_per_s=refill_per_s)


def default_limits() -> dict[str, dict[str, Bucket]]:
    # Starting points to be tuned with Member C's data, not measured truths.
    # Per-IP and per-subnet buckets are deliberately generous: a campus NAT puts
    # thousands of legitimate students behind one address.
    def shape(identity: Bucket, device: Bucket, ip: Bucket, subnet: Bucket) -> dict[str, Bucket]:
        return {"identity": identity, "device": device, "ip": ip, "subnet": subnet}

    return {
        "enter": shape(_b(3, 0.3), _b(6, 0.6), _b(300, 50), _b(1500, 250)),
        "claim": shape(_b(3, 0.3), _b(6, 0.6), _b(300, 50), _b(1500, 250)),
        "status": shape(_b(2, 0.5), _b(4, 1.0), _b(600, 100), _b(3000, 500)),
        "challenge": shape(_b(5, 0.5), _b(8, 0.8), _b(300, 50), _b(1500, 250)),
    }


class RateLimitLayer(_Strict):
    enabled: bool = False
    # An endpoint or dimension that is omitted is simply not limited on that axis.
    limits: dict[Endpoint, dict[Dimension, Bucket]] = Field(default_factory=default_limits)
    # Retry-After is stretched by a random 0..retry_jitter_max fraction so rejected
    # clients do not all come back in the same millisecond.
    retry_jitter_max: float = Field(default=0.25, ge=0, le=0.25)


# ------------------------------------------------------------------------ pow
class PowLayer(_Strict):
    enabled: bool = False
    # always: challenge every entrant at base difficulty (+ load bits).
    # risk:   challenge only entrants whose risk score reaches risk.thresholds.challenge.
    mode: ChallengeMode = "always"
    base_bits: int = Field(default=14, ge=0, le=32)
    max_bits: int = Field(default=24, ge=0, le=40)
    risk_bits_max: int = Field(default=6, ge=0, le=16)  # extra bits at risk score 1.0
    load_bits_max: int = Field(default=2, ge=0, le=8)  # extra bits at full load
    # Gate calls per second (per event, whole cluster) that count as "full load" for the adaptive bits.
    load_ref_rps: int = Field(default=200, ge=1, le=1_000_000)
    ttl_s: int = Field(default=60, ge=10, le=300)
    single_use: bool = True  # best-effort Redis SETNX; replay is harmless anyway

    @model_validator(mode="after")
    def _bounds(self) -> "PowLayer":
        if self.base_bits > self.max_bits:
            raise ValueError("pow.base_bits must be <= pow.max_bits")
        return self


# -------------------------------------------------------------------- captcha
class CaptchaLayer(_Strict):
    enabled: bool = False
    mode: ChallengeMode = "risk"
    provider: Literal["mock", "turnstile"] = "mock"
    site_key: str = Field(default="", max_length=256)  # public by design; the secret is env-only

    @model_validator(mode="after")
    def _site_key(self) -> "CaptchaLayer":
        if self.enabled and self.provider != "mock" and not self.site_key:
            raise ValueError("captcha.site_key is required for real providers")
        return self


# -------------------------------------------------------------------- signals
def default_signal_weights() -> dict[str, float]:
    # weight = the largest contribution this signal can make to the risk score.
    return {
        "timing_regularity": 0.35,
        "header_anomaly": 0.20,
        "accounts_per_device": 0.45,
        "accounts_per_ip": 0.15,
        "accounts_per_subnet": 0.10,
        "accounts_per_asn": 0.10,
        "account_age": 0.15,
        "registration_velocity": 0.25,
        "email_pattern": 0.30,
        "email_entropy": 0.10,
        "otp_latency": 0.15,
    }


class SignalsLayer(_Strict):
    enabled: bool = False
    weights: dict[SignalName, float] = Field(default_factory=default_signal_weights)
    # IP/subnet/ASN signals are soft evidence (shared campus NAT): together they
    # can never contribute more than this to the score.
    ip_only_cap: float = Field(default=0.15, ge=0, le=1)  # measured: 0.25 left only 0.05 headroom and challenged 0.32% of legitimate students (docs/DEFENCES.md)
    timing_min_samples: int = Field(default=5, ge=3, le=100)

    @model_validator(mode="after")
    def _weights(self) -> "SignalsLayer":
        for name, w in self.weights.items():
            if not 0 <= w <= 1:
                raise ValueError(f"signals.weights.{name} must be within [0, 1]")
        return self


# ----------------------------------------------------------------------- risk
class RiskThresholds(_Strict):
    """Score >= threshold selects that response. Scores below `challenge` → ALLOW at 1.0.
    There is deliberately no reject threshold: REJECT is for hard evidence only."""

    challenge: float = Field(default=0.30, ge=0, le=1)
    weight_half: float = Field(default=0.55, ge=0, le=1)
    weight_quarter: float = Field(default=0.75, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> "RiskThresholds":
        if not self.challenge < self.weight_half < self.weight_quarter:
            raise ValueError("risk.thresholds must satisfy challenge < weight_half < weight_quarter")
        return self


class RiskLayer(_Strict):
    enabled: bool = False
    thresholds: RiskThresholds = Field(default_factory=RiskThresholds)


# ------------------------------------------------------------------- top level
class Layers(_Strict):
    rate_limit: RateLimitLayer = Field(default_factory=RateLimitLayer)
    pow: PowLayer = Field(default_factory=PowLayer)
    captcha: CaptchaLayer = Field(default_factory=CaptchaLayer)
    signals: SignalsLayer = Field(default_factory=SignalsLayer)
    risk: RiskLayer = Field(default_factory=RiskLayer)


class DefencesConfig(_Strict):
    preset: PresetName = "none"
    layers: Layers = Field(default_factory=Layers)
    # What to do when Redis is unreachable:
    #   local  - best-effort per-process limiter (default; weaker but never blocks everyone)
    #   open   - no rate limiting at all
    #   closed - 503 on rate-limited endpoints (protects the DB, sacrifices availability)
    # Allocation integrity never depends on Redis in any mode.
    fail_mode: FailMode = "local"

    @model_validator(mode="after")
    def _cross_layer(self) -> "DefencesConfig":
        L = self.layers
        if L.risk.enabled and not L.signals.enabled:
            raise ValueError("layers.risk requires layers.signals (a score needs inputs)")
        if L.pow.enabled and L.pow.mode == "risk" and not L.risk.enabled:
            raise ValueError("layers.pow.mode='risk' requires layers.risk.enabled")
        if L.captcha.enabled and L.captcha.mode == "risk" and not L.risk.enabled:
            raise ValueError("layers.captcha.mode='risk' requires layers.risk.enabled")
        return self
