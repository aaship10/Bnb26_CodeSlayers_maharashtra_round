"""Integration hooks for Member B (defences).

entry_gate
----------
Called inside POST /events/{id}/enter after authentication, AFTER the
already-entered fast path (duplicates never reach the gate), and before the
entry row is inserted:

    async def my_gate(ctx: GateContext) -> GateDecision

Install it from a plugin's setup(app) with `register_entry_gate(my_gate)`
(see app/plugins.py). The default allows everyone with weight 1.0.

Mapping to HTTP:
  ALLOW     -> entry is created with decision.weight and decision.risk
  CHALLENGE -> 403 CHALLENGE_REQUIRED, details.challenge = decision.challenge
  REJECT    -> 403 REJECTED (decision.reason is logged server-side only; it is
               not echoed to the client so bots do not learn the rule)

If the gate raises, the request fails with 500 INTERNAL and no entry is
created (fail closed).

Weights feed the weighted draw and must come from ALLOWED_WEIGHTS so that the
in-browser verifier reproduces results bit-for-bit (see docs/DRAW_SPEC.md).
Weight 0 means "entered but never drawn" (recorded for transparency).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from fastapi import Request

from app.auth import CurrentUser
from app.services.events import EventInfo

ALLOWED_WEIGHTS: tuple[float, ...] = (1.0, 0.5, 0.25, 0.0)


class GateAction(StrEnum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    CHALLENGE = "CHALLENGE"


@dataclass(frozen=True)
class GateContext:
    user: CurrentUser
    event: EventInfo          # includes event.config, B's opaque defence settings
    request: Request          # headers; request.state.client_ip set by B's middleware
    server_now: datetime
    already_entered: bool     # always False today (fast path runs first); kept for B


@dataclass(frozen=True)
class GateDecision:
    action: GateAction = GateAction.ALLOW
    weight: float = 1.0
    reason: str = ""
    risk: dict[str, Any] = field(default_factory=dict)
    challenge: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.weight not in ALLOWED_WEIGHTS:
            raise ValueError(f"GateDecision.weight must be one of {ALLOWED_WEIGHTS}, got {self.weight!r}")
        if not isinstance(self.risk, dict):
            raise ValueError("GateDecision.risk must be a dict")


EntryGate = Callable[[GateContext], Awaitable[GateDecision]]


async def default_entry_gate(ctx: GateContext) -> GateDecision:
    return GateDecision(action=GateAction.ALLOW, weight=1.0, reason="default-allow")


_entry_gate: EntryGate = default_entry_gate


def register_entry_gate(gate: EntryGate) -> None:
    global _entry_gate
    _entry_gate = gate


def reset_entry_gate() -> None:
    register_entry_gate(default_entry_gate)


async def entry_gate(ctx: GateContext) -> GateDecision:
    decision = await _entry_gate(ctx)
    if not isinstance(decision, GateDecision):
        raise TypeError(f"entry gate returned {type(decision).__name__}, expected GateDecision")
    return decision
