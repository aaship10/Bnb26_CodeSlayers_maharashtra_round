"""Chaos orchestration (E7): run MY open-loop load while an external command injects a fault at a
chosen offset, then require the invariants to hold.

Member B's `infra/chaos/chaos.py` is a self-contained harness (its own load, one fault, its own
checks), so it cannot run alongside this load without doubling it. What B also provides are
inject-only commands (`python infra/local/run.py kill <port>` / `heal`, `fd.ps1 kill|heal`), and
those are what a ChaosStep calls. B's own results (infra/chaos/results/*.json) stay B's evidence.

Outage figures come from THIS run's request timeline, not from the fault command's say-so. Requests are
bucketed by COMPLETION time (when a failure is observed) at 100 ms resolution:
  requests_failed   5xx + timeouts + connection errors over the run
  outage_seconds    the longest stretch, from the fault on, with NO successful response that contains a
                    failure (clients that back off leave silent buckets inside it, so silence counts; silence
                    after recovery does not, because the stretch ends at the next success)
  recovered         False if the run ended while that stretch was still open
  degraded_seconds  whole seconds from the fault on whose failure share is above DEGRADED_SHARE
  recovery_seconds  from the heal command finishing to the end of the last failure
                    (0 if failures stopped before the heal; None if nothing failed)
A partial failure (one replica of several) leaves successes between the failures, so it is NOT an outage.
Resolution is 0.1 s."""
from __future__ import annotations

import asyncio
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.engine.run import TargetConfig, TimedHook, execute_run
from fairdrop_sim.models.scenario import Scenario

DEGRADED_SHARE = 0.05
FAILURE_OUTCOMES = ("5xx", "TIMEOUT", "CONN_ERROR")
OUTPUT_TAIL = 600


@dataclass
class ChaosStep:
    """One command at one offset (seconds after the window opens). `argv` is run without a shell."""

    name: str
    at_s: float
    argv: list[str]
    cwd: str | None = None
    timeout_s: float = 60.0
    role: str = "inject"  # inject | heal (heal marks when recovery is measured from)


def command_hook(step: ChaosStep) -> TimedHook:
    async def fn() -> dict[str, Any]:
        t0 = time.perf_counter()
        proc = await asyncio.to_thread(subprocess.run, step.argv, capture_output=True, text=True,
                                       timeout=step.timeout_s, cwd=step.cwd)
        out = {"argv": step.argv, "exit_code": proc.returncode, "seconds": round(time.perf_counter() - t0, 3),
               "stdout": (proc.stdout or "")[-OUTPUT_TAIL:], "stderr": (proc.stderr or "")[-OUTPUT_TAIL:]}
        if proc.returncode != 0:
            raise RuntimeError(f"{step.name} exited {proc.returncode}: {(proc.stderr or proc.stdout or '')[-200:]}")
        return out

    return TimedHook(step.name, step.at_s, fn)


def failure_timeline(rec: Recorder) -> dict[int, tuple[int, int]]:
    """tenth of a second since the window opened (by completion time) -> (ok, failed)."""
    per: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for key, n in rec.completions.items():
        tenth, kind = key.split("|", 1)
        per[int(tenth)][1 if kind == "fail" else 0] += n
    return {t: (v[0], v[1]) for t, v in per.items()}


def chaos_metrics(timeline: dict[int, tuple[int, int]], fault_at_s: float, heal_done_s: float | None) -> dict[str, Any]:
    """Pure function over the per-0.1 s (ok, failed) timeline; see the module docstring."""
    first = int(fault_at_s * 10)
    failed_total = sum(f for _, f in timeline.values())
    bad = sorted(t for t, (_, f) in timeline.items() if f > 0)
    last_t = max(timeline) if timeline else 0

    longest, recovered = 0, True
    t = first
    while t <= last_t:
        ok, f = timeline.get(t, (0, 0))
        if f > 0 and ok == 0:  # a stretch with no success begins at a failure
            start = t
            while t <= last_t and timeline.get(t, (0, 0))[0] == 0:
                t += 1
            if t > last_t:
                recovered = False
            longest = max(longest, t - start)
        else:
            t += 1

    per_second: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for tt, (ok, f) in timeline.items():
        if tt >= first:
            per_second[tt // 10][0] += ok
            per_second[tt // 10][1] += f
    degraded = sum(1 for ok, f in per_second.values() if ok + f > 0 and f / (ok + f) > DEGRADED_SHARE)

    recovery = None
    if bad and heal_done_s is not None:
        recovery = round(max(0.0, (bad[-1] + 1) / 10 - heal_done_s), 2)
    return {"requests_failed": failed_total, "outage_seconds": round(longest / 10, 1), "recovered": recovered,
            "degraded_seconds": degraded, "recovery_seconds": recovery,
            "first_failure_s": bad[0] / 10 if bad else None, "last_failure_s": bad[-1] / 10 if bad else None,
            "requests_during_fault": sum(ok + f for tt, (ok, f) in timeline.items() if tt >= first)}


@dataclass
class ChaosResult:
    summary: dict[str, Any]
    chaos: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return (bool(self.chaos.get("invariants_passed")) and bool(self.chaos.get("steps_ok"))
                and bool(self.chaos.get("ordering_ok", True)))


async def run_chaos(sc: Scenario, steps: list[ChaosStep], target: TargetConfig, out_dir: Path,
                    run_index: int = 0, log=print) -> ChaosResult:
    summary = await execute_run(sc, run_index, target, out_dir=out_dir, log=log,
                                hooks=[command_hook(s) for s in steps])
    hooks = {h["name"]: h for h in summary.get("hooks", [])}
    inject = [s for s in steps if s.role == "inject"]
    heal = [s for s in steps if s.role == "heal"]
    fault_at = min((s.at_s for s in inject), default=0.0)
    heal_done = None
    if heal and heal[-1].name in hooks and hooks[heal[-1].name].get("finished_at_s") is not None:
        heal_done = hooks[heal[-1].name]["finished_at_s"]
    from fairdrop_sim.metrics.loader import load_run

    rec = load_run(summary["artifacts"]).recorder
    m = chaos_metrics(failure_timeline(rec), fault_at, heal_done)
    inject_done = max((hooks.get(s.name, {}).get("finished_at_s") for s in inject
                       if hooks.get(s.name, {}).get("finished_at_s") is not None), default=None)
    heal_start = min((hooks.get(s.name, {}).get("started_at_s") for s in heal
                      if hooks.get(s.name, {}).get("started_at_s") is not None), default=None)
    ordering_ok = not (inject_done is not None and heal_start is not None and heal_start < inject_done)
    chaos = {
        **m, "fault_at_s": fault_at, "heal_done_s": heal_done, "inject_done_s": inject_done,
        "ordering_ok": ordering_ok,
        "invariants_passed": bool(summary["integrity"]["passed"]),
        "invariant_checks": {k: v for k, v in summary["integrity"].items() if k not in ("passed", "draw_verified")},
        "steps_ok": all(hooks.get(s.name, {}).get("ok") for s in steps),
        "steps": [{"name": s.name, "role": s.role, "planned_at_s": s.at_s, **{
            k: hooks.get(s.name, {}).get(k) for k in ("started_at_s", "finished_at_s", "ok", "error")}}
            for s in steps],
        "entry_only": bool(summary.get("entry_only")),
    }
    summary["chaos"] = chaos
    (Path(summary["artifacts"]) / "chaos.json").write_text(
        __import__("json").dumps(chaos, indent=2, default=str) + "\n", encoding="utf-8")
    return ChaosResult(summary, chaos)
