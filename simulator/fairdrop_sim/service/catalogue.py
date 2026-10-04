"""Scenario catalogue behind GET /scenarios and POST /runs.

The ids and flat parameter names (legit_users, bots, request_multiplier, inventory, mode,
defence_preset, bot_identities, kill_at_s) are exactly what D's panel already sends, so its
demo presets work unchanged. Each scenario maps those params onto a full Scenario.

DEMO SCALE, stated honestly: runs are time-compressed so a preset finishes in about two
minutes. The entry window is 6 s (not 10 minutes), 2,000 logical users, 100 seats, and human
think-times, CAPTCHA solve time and PoW difficulty are compressed to match (PoW 10 bits, human
CAPTCHA ~1 s). Request RATES are not compressed. Full-scale numbers come from the E1-E8
experiments against the real target (Stage 7). Defence layers on the mock are toys
(mock_server/defences.py); only the real stack's numbers are evidence.

Option A (decided with the team): the FCFS vs Fair Drop attack uses >= 1,000 bot identities,
more than the seats, so FCFS can honestly approach 100% while the lottery gives about the
bots' share of entrants.
"""
from __future__ import annotations

import os


from typing import Any

from fairdrop_sim.models import Scenario

DEFENCE_PRESETS = ["none", "rate_limit", "rate_limit+pow", "rate_limit+pow+captcha", "all"]
PER_REPEAT_FIXED_S = 20  # lead 2 + window 6 + draw 1 + claim phase 5 + shard start-up/coordination
DEMO_POW_BITS = 10  # ~1k hashes: real work, but cheap enough that 2,000 humans can solve in a 6 s window


def _int(title: str, lo: int, hi: int, default: int, description: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "integer", "title": title, "minimum": lo, "maximum": hi, "default": default}
    if description:
        d["description"] = description
    return d


LEGIT = _int("Real people", 100, 50_000, 2_000,
             "Logical users simulated as async clients (not simultaneous sockets). Demo default 2,000.")
INVENTORY = _int("Seats", 1, 5_000, 100)
MODE = {"type": "string", "title": "Allocation mode", "enum": ["LOTTERY", "FCFS"], "default": "LOTTERY"}


def _preset(default: str) -> dict[str, Any]:
    return {"type": "string", "title": "Defence preset", "enum": DEFENCE_PRESETS, "default": default}


SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "flash_crowd",
        "name": "Flash crowd, no attack",
        "description": "Everyone arrives in the first seconds. The baseline for latency and fairness without bots.",
        "profile": "baseline",
        "properties": {"legit_users": LEGIT, "inventory": INVENTORY, "mode": MODE},
        "required": ["legit_users", "inventory", "mode"],
    },
    {
        "id": "bot_swarm",
        "name": "Fast bots",
        "description": ("A thousand bot identities hammering the API far faster than a person can. "
                        "More identities than seats, so first-come-first-served can be swept."),
        "profile": "attack",
        "properties": {
            "legit_users": LEGIT,
            "bots": _int("Bot identities", 0, 10_000, 1_000),
            "request_multiplier": {"type": "number", "title": "Bot request rate (x a person)", "minimum": 1,
                                   "maximum": 1000, "default": 100},
            "inventory": INVENTORY,
            "mode": MODE,
            "defence_preset": _preset("rate_limit+pow"),
        },
        "required": ["legit_users", "bots", "request_multiplier", "inventory", "mode", "defence_preset"],
    },
    {
        "id": "sybil_farm",
        "name": "Sybil farm",
        "description": ("One attacker controlling many accounts, each entering once and solving every "
                        "challenge it is given. Defences make each identity cost more."),
        "profile": "attack",
        "properties": {
            "legit_users": LEGIT,
            "bot_identities": _int("Sybil identities", 1, 20_000, 1_000),
            "bot_ips": _int("Distinct IPs the farm uses", 1, 20_000, 1,
                            "1 = the whole farm shares one address (easy to cluster); raise it to evade IP signals."),
            "inventory": INVENTORY,
            "mode": MODE,
            "defence_preset": _preset("rate_limit+pow"),
        },
        "required": ["legit_users", "bot_identities", "inventory", "mode", "defence_preset"],
    },
    {
        "id": "replica_kill",
        "name": "Kill a replica mid-run",
        "description": ("One API replica is killed during the entry window; state must survive. "
                        "Needs Member B's chaos scripts and the real multi-replica stack."),
        "profile": "chaos",
        "properties": {
            "legit_users": LEGIT,
            "bots": _int("Bot identities", 0, 10_000, 200),
            "kill_at_s": {"type": "number", "title": "Kill a replica after (s)", "minimum": 1, "maximum": 60,
                          "default": 10},
            "inventory": INVENTORY,
            "mode": MODE,
        },
        "required": ["legit_users", "kill_at_s", "inventory", "mode"],
        "runnable": False,
        "unavailable_reason": ("Chaos runs need Member B's infra/chaos scripts and the real multi-replica "
                               "stack. They arrive with the real-target integration."),
    },
]
BY_ID = {s["id"]: s for s in SCENARIOS}


class ParamError(ValueError):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


def defaults_of(scenario_id: str) -> dict[str, Any]:
    return {k: v.get("default") for k, v in BY_ID[scenario_id]["properties"].items()}


def validate_params(scenario_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """Defaults + overrides, validated against the scenario's schema. Raises ParamError."""
    props = BY_ID[scenario_id]["properties"]
    for k, v in overrides.items():
        s = props.get(k)
        if s is None:
            raise ParamError(f"unknown parameter: {k}", k)
        t = s.get("type")
        if t == "integer" and not (isinstance(v, int) and not isinstance(v, bool)):
            raise ParamError(f"{k} must be a whole number", k)
        if t == "number" and not (isinstance(v, (int, float)) and not isinstance(v, bool)):
            raise ParamError(f"{k} must be a number", k)
        if t == "boolean" and not isinstance(v, bool):
            raise ParamError(f"{k} must be true or false", k)
        if t == "string" and not isinstance(v, str):
            raise ParamError(f"{k} must be text", k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in s and v < s["minimum"]:
                raise ParamError(f"{k} must be at least {s['minimum']}", k)
            if "maximum" in s and v > s["maximum"]:
                raise ParamError(f"{k} must be at most {s['maximum']}", k)
        if "enum" in s and v not in s["enum"]:
            raise ParamError(f"{k} must be one of {', '.join(map(str, s['enum']))}", k)
    return {**defaults_of(scenario_id), **overrides}


def public_view(s: dict[str, Any]) -> dict[str, Any]:
    """The GET /scenarios item (what D's zod scenarioSchema reads)."""
    est = estimate_duration_s(s["id"], defaults_of(s["id"]), 5)
    return {
        "id": s["id"],
        "name": s["name"],
        "description": s["description"],
        "profile": s["profile"],
        "params_schema": {"type": "object", "properties": s["properties"], "required": s["required"]},
        "estimated_duration_s": est,
        "scale": f"{defaults_of(s['id'])['legit_users']:,} logical users"
                 + (" + bots" if "bots" in s["properties"] or "bot_identities" in s["properties"] else "")
                 + " (demo scale, time-compressed; 5 repeats)",
    }


def estimate_duration_s(scenario_id: str, params: dict[str, Any], repeats: int) -> float:
    extra = max(0.0, (int(params.get("legit_users", 0)) - 5_000) / 2_000)
    return round(repeats * (PER_REPEAT_FIXED_S + extra), 1)


def build_scenario(scenario_id: str, params: dict[str, Any], *, seed: int, repeats: int, target: str,
                   name: str | None = None) -> Scenario:
    """Map a scenario's flat params onto a full Scenario (event + crowd + attacker + load)."""
    p = params
    mode = p.get("mode", "LOTTERY")
    preset = p.get("defence_preset", "none" if scenario_id == "flash_crowd" else "rate_limit+pow")
    legit = int(p["legit_users"])
    attackers: list[dict[str, Any]] = []
    # The real target is slower than the in-memory mock (a hosted Postgres adds round trips to every
    # request), so its window is configurable: SIM_REAL_WINDOW_S (default 6, the mock's demo window).
    real = target == "real"
    window_s = float(os.environ.get("SIM_REAL_WINDOW_S", "6")) if real else 6.0
    # Named presets are expanded by the real target itself (its parameter names differ from the mock's).
    defence_layers = None if real else {
        "pow": {"difficulty_bits": DEMO_POW_BITS},
        "rate_limit": {"per_ip_rps": 50, "per_ip_burst": 100, "per_user_rps": 20, "per_user_burst": 40},
    }

    if scenario_id == "bot_swarm" and int(p["bots"]) > 0:
        n = int(p["bots"])
        attackers.append({
            "profile": "retry_spammer", "identities": n, "ips": max(1, n // 5),
            "rps_per_identity": 0.5, "request_multiplier": float(p["request_multiplier"]),
            "request_budget": 10, "poll_interval_s": max(1.0, n / 1500),
        })
    elif scenario_id == "sybil_farm":
        n = int(p["bot_identities"])
        attackers.append({
            "profile": "sybil_single_ip", "identities": n, "ips": int(p.get("bot_ips", 1)),
            "rps_per_identity": 1.0, "solves_pow": True, "solves_captcha": True,
            "hash_rate": 2e7, "captcha_solve_s_mean": 0.5, "poll_interval_s": max(1.0, n / 1500),
        })

    return Scenario.model_validate({
        "name": name or f"svc_{scenario_id}",
        "description": next(s["name"] for s in SCENARIOS if s["id"] == scenario_id),
        "target": target,
        "seed": seed,
        "repeats": max(2, repeats),  # the Scenario model wants >= 2; the service enforces its own repeats
        "event": {
            "inventory": int(p["inventory"]), "window_seconds": window_s, "claim_ttl_seconds": 4, "mode": mode,
            "defences": {"preset": preset, **({"layers": defence_layers} if defence_layers else {})},
        },
        "legit": {
            "count": legit,
            "arrival": {"kind": "spike_tail", "spike_fraction": 0.6, "spike_window_fraction": 0.08},
            "nat_groups": {"fraction": 0.1, "group_size": 50},
            "poll": {"interval_s_mean": 1.0, "max_polls": 8},
            "claim": {"delay_lognormal_mu": -1.0, "delay_lognormal_sigma": 0.4, "no_show_rate": 0.03},
            "device": {"pow_mode": "modelled_delay", "hash_rate_lognormal_mu": 13.8,
                       "captcha_solve_s_mean": 1.0, "captcha_fail_rate": 0.02},
        },
        "attackers": attackers,
        "load": {"max_in_flight": 800, "procs": 4 if legit > 5_000 else 2, "lead_s": 2, "claim_phase_s": 5,
                 "request_timeout_s": 8},
        "auth_mode": "dev_header",
    })
