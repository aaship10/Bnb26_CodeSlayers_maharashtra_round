"""Scenario config (YAML, validated here). Section 8 of the Member C brief.

A scenario fully describes one experiment cell: the event, the legitimate
crowd, the attackers and the load shape. Together with `seed` it makes a run
reproducible; the resolved scenario is stored inside every Results file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Target = Literal["real", "mock"]
Mode = Literal["LOTTERY", "FCFS"]
DefencePreset = Literal["none", "rate_limit", "rate_limit+pow", "rate_limit+pow+captcha", "all", "custom"]
DEFENCE_LAYERS = ("rate_limit", "pow", "captcha", "signals", "risk")

AttackProfile = Literal[
    "naive_flooder",
    "speed_bot",
    "retry_spammer",
    "sybil_single_ip",
    "distributed_botnet",
    "human_mimic",
    "late_flooder",
    "window_edge_bot",
    "claim_sniper",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DefenceConfig(_Strict):
    """Mirrors events.config.defences. `layers` is only required for preset=custom;
    for named presets the server expands the preset and `layers` may override params."""

    preset: DefencePreset = "none"
    layers: dict[str, dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _check(self) -> "DefenceConfig":
        if self.preset == "custom" and not self.layers:
            raise ValueError("preset=custom needs layers")
        for name in self.layers or {}:
            if name not in DEFENCE_LAYERS:
                raise ValueError(f"unknown defence layer {name!r}; expected one of {DEFENCE_LAYERS}")
        return self


class EventConfig(_Strict):
    inventory: int = Field(500, ge=1)
    window_seconds: float = Field(60, gt=0)
    claim_ttl_seconds: float = Field(60, gt=0)
    mode: Mode = "LOTTERY"
    defences: DefenceConfig = DefenceConfig()


class ArrivalModel(_Strict):
    """spike_tail: `spike_fraction` of users arrive in the first `spike_window_fraction`
    of the window with exponential decay (time constant `spike_tau_fraction` of the
    window); the rest arrive uniformly over the whole window. uniform: all uniform."""

    kind: Literal["spike_tail", "uniform"] = "spike_tail"
    spike_fraction: float = Field(0.6, ge=0, le=1)
    spike_window_fraction: float = Field(0.05, gt=0, le=1)
    spike_tau_fraction: float = Field(0.015, gt=0, le=1)


class RetryModel(_Strict):
    """A fraction of humans refresh/retry enter 1..max_retries extra times with jitter."""

    retry_fraction: float = Field(0.3, ge=0, le=1)
    min_retries: int = Field(1, ge=0)
    max_retries: int = Field(3, ge=0)
    jitter_ms_mean: float = Field(1500, ge=0)

    @model_validator(mode="after")
    def _check(self) -> "RetryModel":
        if self.min_retries > self.max_retries:
            raise ValueError("min_retries > max_retries")
        return self


class NatGroups(_Strict):
    """A fraction of humans share client IPs in groups (campus/CGNAT)."""

    fraction: float = Field(0.1, ge=0, le=1)
    group_size: int = Field(50, ge=1)


class PollModel(_Strict):
    """How humans learn their outcome after the window closes.

    Each human checks /status at draw time + Exp(interval_s_mean), then every
    Exp(interval_s_mean) until the outcome is final or max_polls is reached.
    Waitlisted humans keep checking only if their waitlist position is within
    `waitlist_attention` x inventory (someone 38,000th in line stops refreshing).
    `sse_sample`: the page would get pushed updates over SSE; we sample that state
    with the same GETs, so SSE connection load itself is NOT measured (limitation)."""

    mode: Literal["poll", "sse_sample"] = "sse_sample"
    interval_s_mean: float = Field(10, gt=0)
    max_polls: int = Field(30, ge=1)
    waitlist_attention: float = Field(2.0, ge=0)


class ClaimBehaviour(_Strict):
    """Winners claim after a lognormal delay (seconds); `no_show_rate` never claim."""

    delay_lognormal_mu: float = 2.0  # median ~7.4 s
    delay_lognormal_sigma: float = Field(0.8, ge=0)
    no_show_rate: float = Field(0.05, ge=0, le=1)


class NetworkModel(_Strict):
    """Modelled client-side one-way latency added before each send (ms, lognormal)."""

    latency_lognormal_mu: float = 3.9  # median ~50 ms
    latency_lognormal_sigma: float = Field(0.5, ge=0)


class DeviceModel(_Strict):
    """Human device PoW speed. pow_mode=real solves with this machine's CPU;
    modelled_delay solves for real (the server must verify) but then waits until
    the modelled device time (hashes / hash_rate) has elapsed."""

    pow_mode: Literal["real", "modelled_delay"] = "modelled_delay"
    hash_rate_lognormal_mu: float = 13.8  # median ~1M H/s (desktop browser worker measured ~1.2M)
    hash_rate_lognormal_sigma: float = Field(0.7, ge=0)
    captcha_solve_s_mean: float = Field(8.0, ge=0)
    captcha_fail_rate: float = Field(0.05, ge=0, le=1)


class LegitConfig(_Strict):
    count: int = Field(50_000, ge=0)
    arrival: ArrivalModel = ArrivalModel()
    retry: RetryModel = RetryModel()
    nat_groups: NatGroups = NatGroups()
    poll: PollModel = PollModel()
    claim: ClaimBehaviour = ClaimBehaviour()
    network: NetworkModel = NetworkModel()
    device: DeviceModel = DeviceModel()
    register_sample: int = Field(0, ge=0, description="users that go through the real registration flow")


class AttackerCost(_Strict):
    """Modelled attacker money cost (USD). Explicitly a model, not a measurement."""

    per_account: float = Field(0.10, ge=0)
    per_ip: float = Field(0.50, ge=0)
    per_captcha: float = Field(0.002, ge=0)
    per_1e9_hashes: float = Field(0.01, ge=0)
    per_1e6_requests: float = Field(0.05, ge=0)


class AttackerConfig(_Strict):
    profile: AttackProfile
    identities: int = Field(1, ge=1)
    ips: int = Field(1, ge=1)
    rps_per_identity: float = Field(1.0, gt=0)
    request_multiplier: float = Field(1.0, gt=0, description="E1 knob: scales rps relative to a human")
    start_offset_s: float = 0.0
    duration_s: float | None = Field(None, gt=0)
    timing_jitter_ms: float = Field(0, ge=0)
    # None = use the profile's natural behaviour (profiles.PROFILES); a bool overrides it.
    solves_pow: bool | None = None
    solves_captcha: bool | None = None
    obey_retry_after: bool | None = None
    shared_device: bool | None = None
    pow_mode: Literal["real", "modelled_delay"] = "modelled_delay"
    hash_rate: float = Field(5e6, gt=0, description="attacker H/s per identity (bots run native code)")
    captcha_solve_s_mean: float = Field(2.0, ge=0, description="paid solving-service latency per CAPTCHA")
    captcha_fail_rate: float = Field(0.02, ge=0, le=1)
    cost: AttackerCost = AttackerCost()

    @property
    def effective_rps(self) -> float:
        return self.rps_per_identity * self.request_multiplier


class LoadConfig(_Strict):
    """Load-generator settings. Requests are scheduled open-loop at intended times;
    max_in_flight caps concurrent requests (split evenly across procs) and any wait
    for a slot counts toward latency, which is measured from the intended time."""

    max_in_flight: int = Field(2000, ge=1)
    procs: int = Field(1, ge=1)
    request_timeout_s: float = Field(10, gt=0)
    lead_s: float = Field(5.0, ge=1, description="time between scheduling the event and the window opening")
    draw_delay_s: float = Field(1.0, ge=0, description="admin draw this long after the window closes")
    claim_phase_s: float | None = Field(None, gt=0, description="run end after the draw; default 3 x claim TTL")
    sim_client_ip: bool = Field(True, description="send X-Sim-Client-IP + X-Sim-Key so each client has its own IP")
    keepalive_s: float = Field(15.0, gt=0, description="client idle-connection lifetime; keep below the server's")


class Scenario(_Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    description: str = ""
    event_id: str | None = Field(None, description="target event id; default evt_sim_<name>")
    target: Target = "mock"
    seed: int = Field(20261101, ge=0)
    repeats: int = Field(30, ge=2, description="at least 2: one run has no uncertainty estimate")
    event: EventConfig = EventConfig()
    legit: LegitConfig = LegitConfig()
    attackers: list[AttackerConfig] = []
    load: LoadConfig = LoadConfig()
    auth_mode: Literal["dev_header", "jwt_sim_tokens"] = "dev_header"

    @property
    def bot_identities(self) -> int:
        return sum(a.identities for a in self.attackers)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Scenario":
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))
