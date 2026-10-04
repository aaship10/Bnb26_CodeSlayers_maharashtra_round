"""The 9 attack profiles (section 7). A profile is a BEHAVIOUR (flags + timing); the
SIZE (identities, ips, rps) comes from the scenario's AttackerConfig, so experiments
sweep size without touching behaviour. `suggested` sizing is for docs and the /sim
params form (Stage 6), never applied silently to a scenario.

enter_window: when enter attempts happen
  window      - during the open window, spread by rate
  zero        - every identity fires at t_open exactly (pre-warmed thundering herd)
  edge        - at the last instant before the window closes
  after_close - only after the window has closed (tests WINDOW_CLOSED + load shedding)
enter_mode:
  once  - a few attempts until entered (sequential; can solve challenges)
  flood - many attempts at `rps` for the phase, open-loop (does not solve challenges)
"""
from __future__ import annotations

from dataclasses import dataclass

from fairdrop_sim.models.scenario import AttackProfile


@dataclass(frozen=True)
class BotBehavior:
    enter_window: str = "window"
    enter_mode: str = "once"
    claim: bool = True  # convert a WON hold into a seat
    claim_snipe: bool = False  # flood status+claim through the claim phase
    solves_pow: bool = False
    solves_captcha: bool = False
    obey_retry_after: bool = False
    shared_device: bool = False
    budget: int = 8  # cap on enter attempts per identity (bounds flood volume)


@dataclass(frozen=True)
class Suggested:
    identities: int = 1
    ips: int = 1
    rps_per_identity: float = 5.0


PROFILES: dict[AttackProfile, BotBehavior] = {
    "naive_flooder": BotBehavior(enter_window="window", enter_mode="flood", budget=5000),
    "speed_bot": BotBehavior(enter_window="zero", enter_mode="once", budget=5),
    "retry_spammer": BotBehavior(enter_window="window", enter_mode="flood", budget=50),
    "sybil_single_ip": BotBehavior(enter_window="window", enter_mode="once", budget=4),
    "distributed_botnet": BotBehavior(enter_window="window", enter_mode="once", budget=4),
    "human_mimic": BotBehavior(enter_window="window", enter_mode="once", solves_pow=True,
                               solves_captcha=True, obey_retry_after=True, budget=5),
    "late_flooder": BotBehavior(enter_window="after_close", enter_mode="flood", claim=False, budget=2000),
    "window_edge_bot": BotBehavior(enter_window="edge", enter_mode="once", budget=3),
    "claim_sniper": BotBehavior(enter_window="window", enter_mode="once", claim=True, claim_snipe=True, budget=4),
}

SUGGESTED: dict[AttackProfile, Suggested] = {
    "naive_flooder": Suggested(identities=1, ips=1, rps_per_identity=2000),
    "speed_bot": Suggested(identities=1, ips=1, rps_per_identity=50),
    "retry_spammer": Suggested(identities=500, ips=500, rps_per_identity=5),
    "sybil_single_ip": Suggested(identities=1000, ips=1, rps_per_identity=1),
    "distributed_botnet": Suggested(identities=1000, ips=200, rps_per_identity=1),
    "human_mimic": Suggested(identities=500, ips=500, rps_per_identity=0.3),
    "late_flooder": Suggested(identities=50, ips=50, rps_per_identity=200),
    "window_edge_bot": Suggested(identities=500, ips=500, rps_per_identity=1),
    "claim_sniper": Suggested(identities=500, ips=500, rps_per_identity=20),
}


def behavior_for(profile: AttackProfile, overrides: dict | None = None) -> BotBehavior:
    beh = PROFILES[profile]
    for key in ("solves_pow", "solves_captcha", "obey_retry_after", "shared_device"):
        val = (overrides or {}).get(key)
        if val is not None:
            beh = type(beh)(**{**beh.__dict__, key: val})
    return beh
