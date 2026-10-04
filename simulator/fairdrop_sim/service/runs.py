"""Run lifecycle for the /sim service.

One worker executes runs strictly one at a time: concurrent runs would share the machine
and distort each other's latency, which is exactly what we are measuring. Each run is
`repeats` independent runs of its scenario (fresh reset and a fresh server seed each time),
aggregated into a Results object with CIs.

Every run keeps an append-only event log (integer ids) so the SSE stream can resume with
Last-Event-ID. Metadata and results are persisted so the run list survives a restart
(a run that was queued/running at shutdown is marked failed: "service restarted").
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fairdrop_sim.engine.run import execute_run
from fairdrop_sim.metrics.aggregate import build_results
from fairdrop_sim.service import catalogue as cat
from fairdrop_sim.service.targets import Targets, TargetUnavailable

class ScenarioUnavailable(RuntimeError):
    """The scenario exists but cannot run yet (e.g. chaos needs B's scripts). Message is showable."""


TERMINAL = {"done", "failed", "cancelled"}
PHASE_TEXT = {
    "SCHEDULED": "waiting for the window to open", "OPEN": "entries arriving", "DRAWING": "drawing the winners",
    "CLAIMING": "winners claiming seats", "CLOSED": "closing the event", "DRAFT": "preparing",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Run:
    id: str
    scenario_id: str
    scenario_name: str
    params: dict[str, Any]
    repeats: int
    seed: int
    target: str
    status: str = "queued"
    progress: float = 0.0
    phase_text: str = "Queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    results: dict[str, Any] | None = None
    log: list[tuple[int, str, str]] = field(default_factory=list)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None

    def view(self) -> dict[str, Any]:
        v = {
            "run_id": self.id, "scenario_id": self.scenario_id, "status": self.status,
            "progress": round(self.progress, 3), "phase_text": self.phase_text, "created_at": self.created_at,
            "started_at": self.started_at, "finished_at": self.finished_at, "target": self.target,
        }
        if self.error:
            v["error"] = self.error
        return v

    def summary(self) -> dict[str, Any]:
        return {**self.view(), "scenario_name": self.scenario_name, "params": self.params,
                "repeats": self.repeats, "seed": self.seed}


class RunManager:
    def __init__(self, targets: Targets, data_dir: Path):
        self.targets = targets
        self.dir = data_dir
        self.runs: dict[str, Run] = {}
        self.queue: asyncio.Queue[Run] = asyncio.Queue()
        self._counter = 0
        self._worker: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._load()
        self._worker = asyncio.create_task(self._work())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
        for r in self.runs.values():
            if r.task and not r.task.done():
                r.task.cancel()
        self.targets.stop()

    # ------------------------------------------------------------------ API operations

    async def submit(self, scenario_id: str, overrides: dict[str, Any], repeats: int, seed: int,
                     target: str) -> Run:
        spec = cat.BY_ID[scenario_id]
        if spec.get("runnable", True) is False:
            raise ScenarioUnavailable(spec["unavailable_reason"])
        params = cat.validate_params(scenario_id, overrides)
        if target == "real":
            await self.targets.resolve("real")  # fail fast with a showable sentence if it isn't up
        self._counter += 1
        run = Run(id=f"run_{datetime.now():%y%m%d%H%M%S}_{self._counter:03d}", scenario_id=scenario_id,
                  scenario_name=spec["name"], params=params, repeats=repeats, seed=seed, target=target)
        self.runs[run.id] = run
        await self._emit(run, "status", run.view())
        self._save(run)
        await self.queue.put(run)
        return run

    async def cancel(self, run: Run) -> None:
        if run.status == "queued":
            await self._finish(run, "cancelled", "Cancelled before it started")
        elif run.status == "running" and run.task:
            run.task.cancel()

    def list(self) -> list[Run]:
        return sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)

    # ------------------------------------------------------------------ event log

    async def _emit(self, run: Run, event: str, payload: dict[str, Any]) -> None:
        async with run.cond:
            run.log.append((len(run.log) + 1, event, json.dumps(payload)))
            run.cond.notify_all()

    async def _finish(self, run: Run, status: str, phase: str, error: str | None = None) -> None:
        run.status, run.phase_text, run.error = status, phase, error
        run.finished_at = _now()
        if status == "done":
            run.progress = 1.0
        await self._emit(run, "status", run.view())
        self._save(run)

    # ------------------------------------------------------------------ execution

    async def _work(self) -> None:
        while True:
            run = await self.queue.get()
            if run.status != "queued":
                continue
            run.task = asyncio.create_task(self._execute(run))
            try:
                await run.task
            except asyncio.CancelledError:
                if self._worker and self._worker.cancelled():
                    raise
            except Exception:  # _execute handles its own errors; never let the worker die
                pass

    async def _execute(self, run: Run) -> None:
        try:
            run.status, run.started_at, run.phase_text = "running", _now(), "Starting"
            await self._emit(run, "status", run.view())
            tgt = await self.targets.resolve(run.target)
            run_dirs: list[Path] = []
            out_dir = self.dir / run.id / "runs"
            loop = asyncio.get_running_loop()

            for i in range(run.repeats):
                sc = cat.build_scenario(run.scenario_id, run.params, seed=run.seed, repeats=run.repeats,
                                        target=run.target)
                lead = sc.load.lead_s

                def on_progress(info: dict[str, Any], i: int = i, lead: float = lead) -> None:
                    planned = float(info.get("planned_s") or 1.0)
                    frac = min(0.97, max(0.0, (float(info["t_s"]) + lead) / (lead + planned)))
                    run.progress = (i + frac) / run.repeats
                    run.phase_text = f"Repeat {i + 1} of {run.repeats}: {PHASE_TEXT.get(info.get('phase'), 'running')}"
                    snap = {"t_s": info["t_s"], "entries": info.get("entries"), "repeat": i + 1}
                    if info.get("requests") is not None:
                        snap["requests"] = info["requests"]
                    loop.create_task(self._emit(run, "progress", {
                        "status": run.status, "progress": round(run.progress, 3), "phase_text": run.phase_text}))
                    loop.create_task(self._emit(run, "snapshot", snap))

                summary = await execute_run(sc, i, tgt, out_dir=out_dir, log=lambda _m: None,
                                            progress_cb=on_progress)
                run_dirs.append(Path(summary["artifacts"]))
                run.progress = (i + 1) / run.repeats
                enter = summary["endpoints"]["enter"]["open_loop_ms"]
                await self._emit(run, "snapshot", {
                    "t_s": summary["load"]["duration_s"], "repeat": i + 1,
                    "requests": summary["load"]["requests"], "throughput_rps": summary["load"]["throughput_rps"],
                    "p95_ms": enter["p95"],
                    **({"bot_seat_share": summary["fairness"]["bot_seat_share"]}
                       if summary["fairness"].get("bot_seat_share") is not None else {}),
                })

            run.phase_text = "Computing metrics"
            await self._emit(run, "progress", {"status": "running", "progress": 0.99, "phase_text": run.phase_text})
            results = await loop.run_in_executor(
                None, lambda: build_results(run_dirs, run_id=run.id, scenario_id=run.scenario_id))
            run.results = results.model_dump(mode="json")
            await self._finish(run, "done", "Done")
        except asyncio.CancelledError:
            await self._finish(run, "cancelled", "Cancelled")
            raise
        except TargetUnavailable as e:
            await self._finish(run, "failed", f"Failed: {e}", str(e))
        except Exception as e:  # a failed run is reported, never hidden
            msg = f"{type(e).__name__}: {e}"
            await self._finish(run, "failed", f"Failed: {msg}", msg)

    # ------------------------------------------------------------------ persistence

    def _save(self, run: Run) -> None:
        d = self.dir / run.id
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(run.summary(), indent=2), encoding="utf-8")
        if run.results is not None:
            (d / "results.json").write_text(json.dumps(run.results), encoding="utf-8")

    def _load(self) -> None:
        if not self.dir.exists():
            return
        for d in sorted(self.dir.iterdir()):
            meta = d / "meta.json"
            if not meta.exists():
                continue
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                run = Run(id=m["run_id"], scenario_id=m["scenario_id"], scenario_name=m.get("scenario_name", ""),
                          params=m.get("params", {}), repeats=m.get("repeats", 1), seed=m.get("seed", 0),
                          target=m.get("target", "mock"), status=m["status"], progress=m.get("progress", 0.0),
                          phase_text=m.get("phase_text", ""), created_at=m.get("created_at", _now()),
                          started_at=m.get("started_at"), finished_at=m.get("finished_at"), error=m.get("error"))
                if run.status not in TERMINAL:
                    run.status, run.error = "failed", "service restarted"
                    run.phase_text, run.finished_at = "Failed: the service restarted", _now()
                res = d / "results.json"
                if res.exists():
                    run.results = json.loads(res.read_text(encoding="utf-8"))
                run.log.append((1, "status", json.dumps(run.view())))
                self.runs[run.id] = run
            except (OSError, KeyError, ValueError):
                continue  # a corrupt record is skipped, not fatal
        self._counter = len(self.runs)
