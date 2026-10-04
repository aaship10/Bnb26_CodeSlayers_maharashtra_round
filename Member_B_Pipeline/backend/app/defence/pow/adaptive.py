"""Adaptive difficulty: base bits + extra bits for risk + extra bits for current load.

Monotone in both inputs (more risk or more load never makes the puzzle easier) and capped, so no
combination can price a legitimate phone out: max_bits is the hard ceiling (default 24 = ~16M hashes).
"""
from __future__ import annotations

from ..config_schema import PowLayer


def difficulty(cfg: PowLayer, risk: float = 0.0, load: float = 0.0) -> int:
    risk = min(1.0, max(0.0, risk))
    load = min(1.0, max(0.0, load))
    bits = cfg.base_bits + round(cfg.risk_bits_max * risk) + round(cfg.load_bits_max * load)
    return min(bits, cfg.max_bits)
