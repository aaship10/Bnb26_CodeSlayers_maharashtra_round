"""Integrity aggregation (METRICS.md §5): worst case across all runs. Any violation in
any run makes the whole result red."""
from __future__ import annotations

from fairdrop_sim.models.results import Integrity

COUNTERS = ("oversold", "duplicate_users", "duplicate_seats", "orphaned_holds")


def integrity_across_runs(run_integrities: list[dict]) -> Integrity:
    worst = {c: 0 for c in COUNTERS}
    draw_verified: bool | None = None
    passed = True
    for r in run_integrities:
        for c in COUNTERS:
            worst[c] = max(worst[c], int(r.get(c, 0) or 0))
        dv = r.get("draw_verified")
        if dv is False:
            draw_verified = False
        elif dv is True and draw_verified is not False:
            draw_verified = True
        if not r.get("passed", False):
            passed = False
    passed = passed and all(v == 0 for v in worst.values()) and draw_verified is not False
    return Integrity(draw_verified=draw_verified, passed=passed, **worst)
