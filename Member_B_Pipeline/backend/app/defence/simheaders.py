"""Simulation-only headers (X-Sim-Key, X-Sim-Client-IP, X-User-Id in jwt mode).

They are honoured ONLY when SIMULATION_MODE=true AND X-Sim-Key matches SIM_KEY
(constant-time compare). In every other case they are ignored and the attempt is
logged. This is what lets Member C drive 50k identities from a few IPs without
creating a spoofing hole in a real deployment.
"""
from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Mapping

from .settings import get_settings

log = logging.getLogger("fd.sim")

SIM_HEADERS = ("x-sim-key", "x-sim-client-ip")
_last_warned: dict[str, float] = {}


def warn_throttled(key: str, msg: str, every_s: float = 60.0) -> None:
    """Log at most once per `every_s` per key: a flood of spoofed requests must not
    become a log flood."""
    now = time.monotonic()
    if now - _last_warned.get(key, -every_s) >= every_s:
        _last_warned[key] = now
        log.warning(msg)


def sim_authorized(headers: Mapping[str, str]) -> bool:
    """True iff this request carries a valid simulation key in simulation mode."""
    s = get_settings()
    presented = headers.get("x-sim-key")
    used_sim_header = any(h in headers for h in SIM_HEADERS)
    if not (s.simulation_mode and s.sim_key):
        if used_sim_header:
            warn_throttled("sim-off", "simulation header received but SIMULATION_MODE is off: ignored")
        return False
    if presented and hmac.compare_digest(presented.encode(), s.sim_key.encode()):
        return True
    if used_sim_header:
        warn_throttled("sim-badkey", "simulation header received with missing/invalid X-Sim-Key: ignored")
    return False
