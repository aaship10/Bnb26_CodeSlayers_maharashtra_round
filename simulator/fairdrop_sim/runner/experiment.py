"""Experiment runner (brief §10, Stage 5).

An experiment is a base scenario plus a grid of cells. The common shape is a sweep:
one x-axis parameter takes several values, crossed with several named series (each a set
of overrides). Every cell is run `repeats` times (a fresh reset between runs, so each is
an independent draw), aggregated into a Results file, and the chosen metric is read off to
build one chart dataset. Per-cell Results and the chart dataset are both stored.

Fixed seeds: the base scenario's seed is the master; series share it so a sweep is a
*paired* comparison (same humans and bot identities across arms, only the knob changes).

Integrity: execute_run already calls the invariants endpoint and the draw verifier after
every run; the runner rolls those up and flags the experiment red if any run failed.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from fairdrop_sim.engine.run import TargetConfig, execute_run
from fairdrop_sim.metrics.aggregate import build_results
from fairdrop_sim.models import ChartDataset, Point, Scenario, Series
from fairdrop_sim.models.results import Results
from fairdrop_sim.runner.overrides import apply_overrides, get_by_path


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AxisSpec(_Strict):
    label: str
    param: str  # dotted path into the scenario, swept over `values`
    values: list[Any]
    scale: Literal["linear", "log", "category"] = "linear"


class SeriesSpec(_Strict):
    name: str
    overrides: dict[str, Any] = Field(default_factory=dict)


class BaselineSpec(_Strict):
    name: str
    metric: str  # a Results metric path; drawn as a flat reference per x


class ExperimentSpec(_Strict):
    id: str
    title: str
    description: str = ""
    chart_id: str
    y_label: str = "value"
    kind: Literal["sweep", "runs"] = "sweep"
    base: str | dict  # path to a scenario YAML (relative to the spec) or an inline scenario
    repeats: int = Field(10, ge=1)
    scale: str | None = None
    final: bool = False  # a final experiment refuses target=mock unless --allow-mock
    x: AxisSpec | None = None
    series: list[SeriesSpec] = Field(default_factory=list)
    metric: str = "fairness.bot_seat_share"
    baseline: BaselineSpec | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ExperimentSpec":
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def load_base_scenario(spec: ExperimentSpec, spec_path: Path | None) -> dict:
    if isinstance(spec.base, dict):
        return spec.base
    base_path = Path(spec.base)
    if not base_path.is_absolute() and spec_path is not None:
        base_path = spec_path.parent / base_path
    return yaml.safe_load(base_path.read_text(encoding="utf-8"))


@dataclass
class CellResult:
    series: str
    x: Any
    results: Results
    run_dirs: list[Path]


@dataclass
class ExperimentOutput:
    spec: ExperimentSpec
    cells: list[CellResult] = field(default_factory=list)
    chart: ChartDataset | None = None
    integrity_passed: bool = True
    out_dir: Path | None = None


class MockNotAllowed(RuntimeError):
    pass


def _cell_name(exp_id: str, series: str, x: Any) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in f"{series}_{x}")
    return f"{exp_id}__{safe}"[:60]


def _metric_point(results: Results, metric_path: str, x: Any) -> Point:
    val = get_by_path(results.model_dump(mode="json")["metrics"], metric_path)
    if isinstance(val, dict) and "mean" in val:  # a Stat
        return Point(x=x, y=val["mean"], ci_low=val.get("ci_low"), ci_high=val.get("ci_high"), n=val.get("n"))
    if isinstance(val, (int, float)):
        return Point(x=x, y=float(val))
    raise ValueError(f"metric {metric_path!r} is not a number/Stat: {val!r}")


async def run_experiment(spec: ExperimentSpec, target: TargetConfig, out_root: Path,
                         spec_path: Path | None = None, allow_mock: bool = False,
                         log: Callable[[str], None] = print) -> ExperimentOutput:
    base = load_base_scenario(spec, spec_path)
    base_target = base.get("target", "mock")
    if spec.final and base_target == "mock" and not allow_mock:
        raise MockNotAllowed(f"experiment {spec.id} is final; refuse target=mock without allow_mock")

    out_dir = out_root / spec.id
    out_dir.mkdir(parents=True, exist_ok=True)
    out = ExperimentOutput(spec=spec, out_dir=out_dir)

    cells: list[tuple[str, Any, dict]] = []
    if spec.kind == "sweep":
        assert spec.x is not None, "sweep needs an x axis"
        for sseries in spec.series:
            for xv in spec.x.values:
                ov = {**sseries.overrides, spec.x.param: xv}
                cells.append((sseries.name, xv, ov))
    else:  # "runs": each series is a single cell (no x sweep)
        for sseries in spec.series:
            cells.append((sseries.name, sseries.name, sseries.overrides))

    series_points: dict[str, list[Point]] = {s.name: [] for s in spec.series}
    for series_name, xv, ov in cells:
        name = _cell_name(spec.id, series_name, xv)
        cell_scn = apply_overrides(base, ov)
        cell_scn["name"] = name
        cell_scn["repeats"] = spec.repeats
        sc = Scenario.model_validate(cell_scn)
        log(f"[{spec.id}] cell {series_name} x={xv}: {spec.repeats} runs")

        run_dirs: list[Path] = []
        for i in range(spec.repeats):
            summary = await execute_run(sc, i, target, out_dir=out_dir / "runs", log=lambda _m: None)
            run_dirs.append(Path(summary["artifacts"]))
            if not summary["integrity"]["passed"]:
                out.integrity_passed = False
                log(f"  ! run {i} integrity FAILED")
        results = build_results(run_dirs, run_id=name, scenario_id=f"{spec.id}:{series_name}:{xv}")
        (out_dir / f"{name}.results.json").write_text(
            json.dumps(results.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
        if not results.metrics.integrity.passed:
            out.integrity_passed = False
        out.cells.append(CellResult(series_name, xv, results, run_dirs))
        pt = _metric_point(results, spec.metric, xv)
        series_points[series_name].append(pt)
        log(f"  {spec.metric} = {pt.y:.4f}" + (f" [{pt.ci_low:.4f},{pt.ci_high:.4f}]" if pt.ci_low is not None else ""))

    chart = _build_chart(spec, series_points, base_target, out.cells)
    out.chart = chart
    (out_dir / f"{spec.chart_id}.json").write_text(
        json.dumps(chart.to_json(), indent=2) + "\n", encoding="utf-8")
    log(f"[{spec.id}] done; integrity {'PASSED' if out.integrity_passed else 'FAILED'}; chart -> {spec.chart_id}.json")
    return out


def _build_chart(spec: ExperimentSpec, series_points: dict[str, list[Point]], target: str,
                 cells: list[CellResult]) -> ChartDataset:
    series = [Series(name=name, points=pts) for name, pts in series_points.items() if pts]
    if spec.baseline is not None and spec.x is not None:
        base_pts = []
        for cr in cells:
            if cr.series == spec.series[0].name:  # one baseline point per x
                p = _metric_point(cr.results, spec.baseline.metric, cr.x)
                base_pts.append(Point(x=cr.x, y=p.y))
        if base_pts:
            series.append(Series(name=spec.baseline.name, points=base_pts))
    x_scale = spec.x.scale if spec.x else "category"
    return ChartDataset(
        chart_id=spec.chart_id, title=spec.title, x_label=spec.x.label if spec.x else "",
        y_label=spec.y_label, x_scale=x_scale, series=series,
        notes=(spec.description or None), target=target, synthetic=False,
        experiment_id=spec.id, run_ids=[c.results.run_id for c in cells])
