"""Shared value types for the defence package (the contract with Member A's hook).

GateDecision is validated at construction so that no code path can ever emit a
weight outside {1.0, 0.5, 0.25}; allocation (A) can therefore trust the value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from .timeutil import iso_z

# The only weights the allocation engine understands. 0 means "excluded" and is
# never produced by the gate: exclusion is a REJECT, which creates no entry.
ALLOWED_WEIGHTS: tuple[float, ...] = (1.0, 0.5, 0.25)


class Action(str, Enum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    CHALLENGE = "CHALLENGE"


@dataclass(frozen=True)
class PowParams:
    prefix: str
    difficulty_bits: int
    algo: str = "sha256-lzb"

    def to_dict(self) -> dict[str, Any]:
        return {"algo": self.algo, "prefix": self.prefix, "difficulty_bits": self.difficulty_bits}


@dataclass(frozen=True)
class CaptchaParams:
    provider: str
    site_key: str

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "site_key": self.site_key}


@dataclass(frozen=True)
class Challenge:
    """Wire shape: {id, type, expires_at, pow?, captcha?} (see docs/POW_SPEC.md)."""

    id: str
    type: Literal["pow", "captcha"]
    expires_at: datetime
    pow: PowParams | None = None
    captcha: CaptchaParams | None = None

    def __post_init__(self) -> None:
        if self.type == "pow" and self.pow is None:
            raise ValueError("pow challenge requires pow params")
        if self.type == "captcha" and self.captcha is None:
            raise ValueError("captcha challenge requires captcha params")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type, "expires_at": iso_z(self.expires_at)}
        if self.pow is not None:
            out["pow"] = self.pow.to_dict()
        if self.captcha is not None:
            out["captcha"] = self.captcha.to_dict()
        return out


@dataclass(frozen=True)
class GateDecision:
    action: Action
    weight: float = 0.0  # only meaningful for ALLOW
    reason: str = ""
    risk: dict[str, Any] | None = None  # explainable breakdown; A stores it on the entry
    challenge: Challenge | None = None  # only for CHALLENGE
    layer: str | None = None  # which layer produced a non-ALLOW decision

    def __post_init__(self) -> None:
        if self.action is Action.ALLOW and self.weight not in ALLOWED_WEIGHTS:
            raise ValueError(f"ALLOW weight must be one of {ALLOWED_WEIGHTS}, got {self.weight}")
        if self.action is not Action.ALLOW and self.weight != 0.0:
            raise ValueError("REJECT/CHALLENGE create no entry and must carry weight 0.0")
        if self.action is Action.CHALLENGE and self.challenge is None:
            raise ValueError("CHALLENGE decision requires a challenge")
        if self.action is not Action.CHALLENGE and self.challenge is not None:
            raise ValueError("only CHALLENGE decisions carry a challenge")

    @classmethod
    def allow(cls, weight: float = 1.0, reason: str = "", risk: dict[str, Any] | None = None) -> "GateDecision":
        return cls(Action.ALLOW, weight, reason, risk)

    @classmethod
    def reject(cls, reason: str, layer: str | None = None, risk: dict[str, Any] | None = None) -> "GateDecision":
        return cls(Action.REJECT, 0.0, reason, risk, None, layer)

    @classmethod
    def require_challenge(
        cls, challenge: Challenge, reason: str = "", layer: str | None = None, risk: dict[str, Any] | None = None
    ) -> "GateDecision":
        return cls(Action.CHALLENGE, 0.0, reason, risk, challenge, layer)


@dataclass
class GateContext:
    """What A passes to entry_gate. `user` and `event` are A's own objects/rows;
    the defence code only relies on user.id and event.id / event.config."""

    user: Any
    event: Any
    request: Any  # starlette Request; request.state.client_ip is set by our middleware
    server_now: datetime
    already_entered: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
