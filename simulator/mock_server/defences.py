"""Toy defence layers for the mock. NOT B's implementation, and never used for results.

They exist so the simulator's client code paths (429 handling, challenge solving,
down-weighting) get exercised before B lands. The parameters are deliberately
simple and documented in GET /admin/defence/presets.
"""
from __future__ import annotations

import copy
import hashlib

from fairdrop_sim.challenge.captcha import verify_sim_token
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import Event

CAPTCHA_OK_TOKEN = "mock-captcha-ok"  # same token as D's mock
CHALLENGE_TTL_S = 120.0

LAYER_DEFAULTS: dict[str, dict[str, Any]] = {
    "rate_limit": {"enabled": False, "per_ip_rps": 5.0, "per_ip_burst": 20, "per_user_rps": 2.0, "per_user_burst": 6},
    "pow": {"enabled": False, "difficulty_bits": 16, "endpoints": ["enter"]},
    "captcha": {"enabled": False, "endpoints": ["enter"]},
    "signals": {"enabled": False, "max_users_per_device": 3},
    "risk": {"enabled": False, "ip_users_half": 5, "ip_users_quarter": 20},
}

PRESETS: dict[str, list[str]] = {
    "none": [],
    "rate_limit": ["rate_limit"],
    "rate_limit+pow": ["rate_limit", "pow"],
    "rate_limit+pow+captcha": ["rate_limit", "pow", "captcha"],
    "all": ["rate_limit", "pow", "captcha", "signals", "risk"],
}


def expand_defences(cfg: dict[str, Any]) -> dict[str, Any]:
    """{preset, layers?} -> {preset, layers: {name: {enabled, ...params}}} with all layers present."""
    preset = cfg.get("preset", "none")
    if preset not in PRESETS and preset != "custom":
        from .core import MockError

        raise MockError(400, "VALIDATION_ERROR", f"unknown preset {preset!r}", {"field": "preset"})
    layers = copy.deepcopy(LAYER_DEFAULTS)
    for name in PRESETS.get(preset, []):
        layers[name]["enabled"] = True
    for name, params in (cfg.get("layers") or {}).items():
        if name not in layers:
            from .core import MockError

            raise MockError(400, "VALIDATION_ERROR", f"unknown layer {name!r}", {"field": "layers"})
        layers[name].update(params)
    return {"preset": preset, "layers": layers}


def presets_view() -> list[dict[str, Any]]:
    return [{"id": p, "layers": expand_defences({"preset": p})["layers"]} for p in PRESETS]


@dataclass
class Bucket:
    tokens: float
    last: float


@dataclass
class DefenceState:
    buckets: dict[str, Bucket] = field(default_factory=dict)
    challenges: dict[str, dict[str, Any]] = field(default_factory=dict)
    passed: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # user -> {"pow","captcha"}
    device_users: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    decisions: list[dict[str, Any]] = field(default_factory=list)
    counter: int = 0

    def decide(self, now: float, user_id: str, layer: str, action: str, reason: str, **extra: Any) -> None:
        from .core import iso

        self.decisions.append({"at": iso(now), "user_id": user_id, "layer": layer, "action": action,
                               "reason": reason, **extra})

    # -------------------------------------------------------------- rate limit

    def _take(self, key: str, rate: float, burst: float, now: float) -> float:
        """Token bucket. Returns 0 if allowed, else seconds until a token is available."""
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = Bucket(tokens=burst, last=now)
        b.tokens = min(burst, b.tokens + (now - b.last) * rate)
        b.last = now
        if b.tokens >= 1:
            b.tokens -= 1
            return 0.0
        return (1 - b.tokens) / rate

    def rate_limit(self, ev: Event, now: float, user_id: str, client_ip: str) -> None:
        cfg = ev.defences["layers"]["rate_limit"]
        if not cfg["enabled"]:
            return
        from .core import MockError

        for scope, key, rate, burst in (
            ("ip", f"ip:{client_ip}", cfg["per_ip_rps"], cfg["per_ip_burst"]),
            ("user", f"user:{user_id}", cfg["per_user_rps"], cfg["per_user_burst"]),
        ):
            wait = self._take(key, rate, burst, now)
            if wait > 0:
                ms = max(1, int(wait * 1000 + 0.999))
                self.decide(now, user_id, "rate_limit", "rate_limit", f"{scope} bucket empty", ip=client_ip)
                raise MockError(429, "RATE_LIMITED", "Too many requests", {"retry_after_ms": ms, "scope": scope},
                                {"Retry-After": str(max(1, -(-ms // 1000)))})

    # -------------------------------------------------------------- challenges

    def required(self, ev: Event, endpoint: str, user_id: str) -> str | None:
        layers = ev.defences["layers"]
        for kind in ("pow", "captcha"):
            cfg = layers[kind]
            if cfg["enabled"] and endpoint in cfg["endpoints"] and kind not in self.passed[user_id]:
                return kind
        return None

    def issue(self, ev: Event, kind: str, user_id: str, now: float) -> dict[str, Any]:
        from .core import iso

        self.counter += 1
        cid = f"ch_{self.counter:06d}"
        c: dict[str, Any] = {"id": cid, "type": kind, "user_id": user_id, "expires": now + CHALLENGE_TTL_S}
        if kind == "pow":
            c["prefix"] = f"fd1.{ev.id}.{user_id[:8]}.{self.counter}"
            c["difficulty_bits"] = int(ev.defences["layers"]["pow"]["difficulty_bits"])
        self.challenges[cid] = c
        view: dict[str, Any] = {"id": cid, "type": kind, "expires_at": iso(c["expires"])}
        if kind == "pow":
            view["pow"] = {"algo": "sha256-lzb", "prefix": c["prefix"], "difficulty_bits": c["difficulty_bits"]}
        else:
            view["captcha"] = {"provider": "mock", "site_key": "mock-site-key"}
        return view

    def check(self, ev: Event, endpoint: str, user_id: str, now: float,
              challenge_id: str | None, solution: str | None, sim_key: str | None = None) -> None:
        """Raise CHALLENGE_REQUIRED until every required challenge for this user is solved.
        A solved challenge is single-use and covers this user for the rest of the event."""
        from .core import MockError

        kind = self.required(ev, endpoint, user_id)
        if kind is None:
            return
        reason = None
        if challenge_id and solution is not None:
            c = self.challenges.pop(challenge_id, None)
            ok = (
                c is not None and c["user_id"] == user_id and c["type"] == kind and c["expires"] > now
                and (verify_pow(c["prefix"], solution, c["difficulty_bits"]) if kind == "pow"
                     else (solution == CAPTCHA_OK_TOKEN
                           or (sim_key is not None and verify_sim_token(solution, sim_key, user_id, ev.id, now))))
            )
            if ok:
                self.passed[user_id].add(kind)
                self.decide(now, user_id, kind, "allow", "challenge solved")
                kind = self.required(ev, endpoint, user_id)
                if kind is None:
                    return
            else:
                reason = "invalid_solution"
                self.decide(now, user_id, kind, "challenge", "invalid or expired solution")
        details: dict[str, Any] = {"challenge": self.issue(ev, kind, user_id, now)}
        if reason:
            details["reason"] = reason
        raise MockError(403, "CHALLENGE_REQUIRED", "Challenge required", details)

    # -------------------------------------------------------------- signals / risk

    def observe(self, ev: Event, now: float, user_id: str, device_id: str | None) -> None:
        if device_id:
            self.device_users[device_id].add(user_id)

    def apply_risk(self, ev: Event, now: float) -> None:
        """At draw time: down-weight entries by how many identities share their IP
        (and, with signals on, their device id). Uses the final counts so the result
        does not depend on arrival order. Weights are only 1.0 / 0.5 / 0.25."""
        layers = ev.defences["layers"]
        if not layers["risk"]["enabled"]:
            return
        per_ip: dict[str, int] = defaultdict(int)
        for e in ev.entries.values():
            per_ip[e.client_ip] += 1
        half, quarter = layers["risk"]["ip_users_half"], layers["risk"]["ip_users_quarter"]
        max_dev = layers["signals"]["max_users_per_device"]
        for e in ev.entries.values():
            n = per_ip[e.client_ip]
            level = 2 if n >= quarter else 1 if n >= half else 0
            if layers["signals"]["enabled"]:
                if not e.device_id or len(self.device_users.get(e.device_id, ())) > max_dev:
                    level = min(2, level + 1)
            e.weight = (1.0, 0.5, 0.25)[level]
            e.risk = level / 2
            if level:
                self.decide(now, e.user_id, "risk", "downweight",
                            f"{n} identities on ip", weight=e.weight, ip=e.client_ip)


def leading_zero_bits(d: bytes) -> int:
    bits = 0
    for byte in d:
        if byte == 0:
            bits += 8
            continue
        return bits + 8 - byte.bit_length()
    return bits


def verify_pow(prefix: str, nonce: str, bits: int) -> bool:
    """Server-side check, written independently of the client solver."""
    if not (nonce.isdigit() and 1 <= len(nonce) <= 20 and nonce.isascii()):
        return False
    return leading_zero_bits(hashlib.sha256(f"{prefix}:{nonce}".encode("utf-8")).digest()) >= bits
