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


class MetricSeries(_Strict):
    name: str
    metric: str  # a Results metric path, e.g. detection.precision


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
    # metric: one metric over the x sweep (default). multi_metric: several metrics (`metrics`)
    # over the same cells, one chart series each. latency: pooled p50/p95/p99 of `latency_endpoint`
    # per cell (needs kind "runs"; each cell becomes a series).
    chart_kind: Literal["metric", "latency", "multi_metric"] = "metric"
    metrics: list[MetricSeries] = Field(default_factory=list)
    latency_endpoint: str = "enter"

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
    skipped: list[str] = field(default_factory=list)  # cells whose metric was undefined (no chart point)


class MockNotAllowed(RuntimeError):
    pass


def _cell_name(exp_id: str, series: str, x: Any) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in f"{series}_{x}")
    return f"{exp_id}__{safe}"[:60]


def _metric_point(results: Results, metric_path: str, x: Any) -> Point | None:
    """The chart point for one cell, or None when the metric is undefined for it (null in
    Results, e.g. bot_seat_share when a defence locked everyone out). Anything else that is
    not a number/Stat is a spec error and raises."""
    val = get_by_path(results.model_dump(mode="json")["metrics"], metric_path)
    if val is None:
        return None
    if isinstance(val, dict) and "mean" in val:  # a Stat
        return Point(x=x, y=val["mean"], ci_low=val.get("ci_low"), ci_high=val.get("ci_high"), n=val.get("n"))
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return Point(x=x, y=float(val))
    raise ValueError(f"metric {metric_path!r} is not a number/Stat: {val!r}")


def expand_cells(spec: ExperimentSpec) -> list[tuple[str, Any, dict]]:
    """(series_name, x, overrides) for every cell the runner will execute."""
    cells: list[tuple[str, Any, dict]] = []
    if spec.kind == "sweep":
        assert spec.x is not None, "sweep needs an x axis"
        for sseries in spec.series:
            for xv in spec.x.values:
                cells.append((sseries.name, xv, {**sseries.overrides, spec.x.param: xv}))
    else:  # "runs": each series is a single cell (no x sweep)
        for sseries in spec.series:
            cells.append((sseries.name, sseries.name, dict(sseries.overrides)))
    return cells


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

    cells = expand_cells(spec)

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
        if spec.chart_kind != "metric":
            continue  # these chart kinds are assembled from all cells after the loop
        pt = _metric_point(results, spec.metric, xv)
        if pt is None:
            # Undefined for this cell (e.g. total lockout => 0 seats). Never dropped silently:
            # logged here, kept in out.skipped, and written into the chart's notes.
            out.skipped.append(f"{series_name} x={xv}: {spec.metric} undefined (see that cell's Results notes)")
            log(f"  ! {spec.metric} is undefined for this cell; no chart point (recorded in chart notes)")
            continue
        series_points[series_name].append(pt)
        log(f"  {spec.metric} = {pt.y:.4f}" + (f" [{pt.ci_low:.4f},{pt.ci_high:.4f}]" if pt.ci_low is not None else ""))

    if spec.chart_kind == "latency":
        chart = _latency_chart(spec, base_target, out.cells)
    elif spec.chart_kind == "multi_metric":
        chart = _multi_metric_chart(spec, base_target, out.cells, out.skipped)
    else:
        chart = _build_chart(spec, series_points, base_target, out.cells, out.skipped)
    out.chart = chart
    (out_dir / f"{spec.chart_id}.json").write_text(
        json.dumps(chart.to_json(), indent=2) + "\n", encoding="utf-8")
    meta = {  # the index entry the /sim service lists (GET /experiments)
        "id": spec.id, "title": spec.title, "description": spec.description, "charts": [spec.chart_id],
        "run_ids": [c.results.run_id for c in out.cells], "target": base_target, "synthetic": False,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repeats": spec.repeats, "scale": spec.scale, "integrity_passed": out.integrity_passed,
        "skipped": out.skipped,
    }
    (out_dir / "experiment.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    log(f"[{spec.id}] done; integrity {'PASSED' if out.integrity_passed else 'FAILED'}; chart -> {spec.chart_id}.json")
    return out


def _latency_chart(spec: ExperimentSpec, target: str, cells: list[CellResult]) -> ChartDataset:
    """Pooled percentiles per cell. Percentiles come from one merged HDR histogram, so there
    is no CI to draw; n on each point is the request count behind it (stated in the notes)."""
    if spec.kind != "runs":
        raise ValueError("chart_kind 'latency' needs kind 'runs' (one cell per series)")
    series = []
    for cr in cells:
        pct = cr.results.metrics.system.latency_ms.get(spec.latency_endpoint)
        if pct is None or not pct.n:
            continue
        pts = [Point(x=q, y=float(getattr(pct, q)), n=pct.n) for q in ("p50", "p95", "p99")
               if getattr(pct, q) is not None]
        series.append(Series(name=cr.series, points=pts))
    lag = [c.results.metrics.system.scheduler_lag_ms for c in cells]
    lag_p99 = max((x.p99 for x in lag if x and x.p99 is not None), default=None)
    notes = (spec.description + " " if spec.description else "") + (
        f"Endpoint: {spec.latency_endpoint}. Open-loop latency from the intended send time, pooled "
        "over all repeats (no CI: one merged histogram).")
    if lag_p99 is not None and lag_p99 > 50:
        notes += (f" WARNING: load-generator scheduler lag p99 reached {lag_p99} ms, so these include "
                  "client-side queueing, not only server time.")
    return ChartDataset(chart_id=spec.chart_id, title=spec.title, x_label="Percentile", y_label=spec.y_label,
                        x_scale="category", series=series, notes=notes, target=target, synthetic=False,
                        experiment_id=spec.id, run_ids=[c.results.run_id for c in cells])


def _multi_metric_chart(spec: ExperimentSpec, target: str, cells: list[CellResult],
                        skipped: list[str]) -> ChartDataset:
    if not spec.metrics:
        raise ValueError("chart_kind 'multi_metric' needs `metrics`")
    series = []
    many = len(spec.series) > 1
    for ss in spec.series:
        for m in spec.metrics:
            pts = []
            for cr in cells:
                if cr.series != ss.name:
                    continue
                pt = _metric_point(cr.results, m.metric, cr.x)
                if pt is None:
                    skipped.append(f"{ss.name} x={cr.x}: {m.metric} undefined")
                else:
                    pts.append(pt)
            if pts:
                series.append(Series(name=f"{ss.name}: {m.name}" if many else m.name, points=pts))
    notes = spec.description or ""
    if skipped:
        notes = (notes + " " if notes else "") + "NO POINT for: " + "; ".join(skipped) + "."
    return ChartDataset(chart_id=spec.chart_id, title=spec.title, x_label=spec.x.label if spec.x else "",
                        y_label=spec.y_label, x_scale=spec.x.scale if spec.x else "category", series=series,
                        notes=notes or None, target=target, synthetic=False, experiment_id=spec.id,
                        run_ids=[c.results.run_id for c in cells])


def _build_chart(spec: ExperimentSpec, series_points: dict[str, list[Point]], target: str,
                 cells: list[CellResult], skipped: list[str] | None = None) -> ChartDataset:
    series = [Series(name=name, points=pts) for name, pts in series_points.items() if pts]
    if spec.baseline is not None and spec.x is not None:
        base_pts = []
        for cr in cells:
            if cr.series == spec.series[0].name:  # one baseline point per x
                p = _metric_point(cr.results, spec.baseline.metric, cr.x)
                if p is not None:
                    base_pts.append(Point(x=cr.x, y=p.y))
        if base_pts:
            series.append(Series(name=spec.baseline.name, points=base_pts))
    x_scale = spec.x.scale if spec.x else "category"
    notes = spec.description or ""
    if skipped:
        notes = (notes + " " if notes else "") + "NO POINT for: " + "; ".join(skipped) + "."
    return ChartDataset(
        chart_id=spec.chart_id, title=spec.title, x_label=spec.x.label if spec.x else "",
        y_label=spec.y_label, x_scale=x_scale, series=series,
        notes=notes or None, target=target, synthetic=False,
        experiment_id=spec.id, run_ids=[c.results.run_id for c in cells])
