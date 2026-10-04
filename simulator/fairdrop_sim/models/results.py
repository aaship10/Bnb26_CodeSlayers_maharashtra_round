"""Results JSON, schema_version 1 (section 11). D's dashboard consumes this; keep it stable.

Changes must be additive within schema_version 1. Anything breaking bumps the
version and is announced in docs/INTERFACE_REQUESTS_C.md.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stat(_Model):
    """A mean WITH its 95% CI and the number of independent observations behind it.

    A Stat always has a real interval. A value without one is emitted as a bare
    number (Value below), which D's UI labels "single value, no CI reported".
    The fairness block is Stat-only: a fairness number is never a bare mean."""

    mean: float
    ci_low: float
    ci_high: float
    n: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> "Stat":
        eps = 1e-9 * max(1.0, abs(self.mean))
        if not (self.ci_low - eps <= self.mean <= self.ci_high + eps):
            raise ValueError(f"CI [{self.ci_low}, {self.ci_high}] does not contain mean {self.mean}")
        return self


Value = Stat | float  # Stat when a CI exists, else a bare number


class Population(_Model):
    legit: int
    bots: int = Field(description="number of attacker instances (profiles x configs)")
    bot_identities: int


class EventSummary(_Model):
    inventory: int
    mode: Literal["LOTTERY", "FCFS"]
    defences: dict[str, Any]


class AttackerCostPerSeat(_Model):
    """null = not applicable or unbounded (bots won no seats); see METRICS.md."""

    requests: Value | None = None
    accounts: Value | None = None
    pow_hashes: Value | None = None
    captcha_solves: Value | None = None
    usd_modelled: Value | None = None


class Fairness(_Model):
    bot_seat_share: Stat
    bot_entrant_share: Stat
    human_win_prob: Stat
    human_entry_success_rate: Stat
    arrival_time_correlation: Stat | None = Field(None, description="null if there were no entrants")
    arrival_time_perm_p: float | None = Field(None, description="permutation-test p-value, pooled")
    jain_index: Stat | None = Field(None, description="null if no identity won across runs (undefined)")
    gini: Stat | None = Field(None, description="null if no identity won across runs (undefined)")
    attacker_cost_per_seat: AttackerCostPerSeat = AttackerCostPerSeat()


class Percentiles(_Model):
    """Pooled across all repeats (one HDR histogram). n = number of requests.
    Percentiles are null only when n = 0 (no request of that kind was sent)."""

    p50: float | None
    p95: float | None
    p99: float | None
    n: int = 0


class ErrorRates(_Model):
    http_429_legit: Value
    http_429_bot: Value
    http_5xx: Value
    timeout: Value


class SystemMetrics(_Model):
    latency_ms: dict[str, Percentiles] = Field(
        description="required keys: enter, status, claim; optional per-class keys such as enter.legit, enter.bot"
    )
    throughput_rps: Value
    error_rates: ErrorRates
    availability: Value | None = Field(None, description="share of non-5xx, non-timeout responses during the spike")
    scheduler_lag_ms: Percentiles | None = Field(
        None, description="load-generator wake-up lag (intended vs actual send). High => latencies "
        "include client-side delay, not just server time; a p99 > 50 ms is flagged in notes.")

    @model_validator(mode="after")
    def _endpoints(self) -> "SystemMetrics":
        missing = {"enter", "status", "claim"} - set(self.latency_ms)
        if missing:
            raise ValueError(f"latency_ms missing {sorted(missing)}")
        return self


class Detection(_Model):
    """null = not applicable (e.g. no defence layer ran)."""

    precision: Value | None = None
    recall: Value | None = None
    false_positive_rate: Value | None = None
    false_positive_rate_nat: Value | None = Field(None, description="FPR restricted to humans in NAT groups")


class Integrity(_Model):
    """Worst case across all runs. Any violation makes passed=false (the run is red)."""

    oversold: int = 0
    duplicate_users: int = 0
    duplicate_seats: int = 0
    orphaned_holds: int = 0
    draw_verified: bool | None = None
    passed: bool


class Metrics(_Model):
    fairness: Fairness
    system: SystemMetrics
    detection: Detection = Detection()
    integrity: Integrity


class PerRun(_Model):
    index: int
    seed: int
    status: Literal["ok", "failed"]
    error: str | None = None
    values: dict[str, float | int | bool | None] = Field(default_factory=dict)


class Results(_Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str
    scenario_id: str
    target: Literal["real", "mock"]
    synthetic: bool
    seed: int
    repeats: int
    failed_runs: int = 0
    created_at: datetime
    population: Population
    event: EventSummary
    metrics: Metrics
    per_run: list[PerRun] | None = None
    notes: list[str] = Field(default_factory=list)
    scenario: dict[str, Any] | None = Field(None, description="the resolved Scenario, for reproducibility")


class Experiment(_Model):
    """GET /experiments item."""

    id: str
    title: str
    description: str
    charts: list[str]
    run_ids: list[str]
    target: Literal["real", "mock"]
    synthetic: bool
    created_at: datetime
