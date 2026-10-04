"""Stage 5 experiment runner: overrides, spec loading, cell expansion, chart building,
one tiny live sweep against the in-process mock, and the chart -> D's zod schema contract."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fairdrop_sim.engine.run import TargetConfig
from fairdrop_sim.metrics.aggregate import build_results
from fairdrop_sim.models import ChartDataset
from fairdrop_sim.models.results import Results
from fairdrop_sim.runner.experiment import (
    CellResult, ExperimentSpec, MockNotAllowed, _build_chart, _metric_point, expand_cells, run_experiment)
from fairdrop_sim.runner.overrides import apply_overrides, get_by_path, set_by_path
from tests.test_engine_e2e import mock_url  # noqa: F401  (module-scoped fixture)
from tests.test_metrics_aggregate import write_run

SIM = Path(__file__).resolve().parents[1]
REPO = SIM.parent
EXPERIMENTS = SIM / "experiments"
CHART_CHECK = Path(__file__).parent / "contract" / "chart_file.check.ts"
TSX = REPO / "frontend" / "node_modules" / ".bin" / ("tsx.cmd" if sys.platform == "win32" else "tsx")


# --- overrides ---------------------------------------------------------------------------

def test_set_get_by_path_dict_and_list_index():
    d = {"event": {"mode": "LOTTERY"}, "attackers": [{"request_multiplier": 1}, {"request_multiplier": 2}]}
    set_by_path(d, "attackers.1.request_multiplier", 100)
    set_by_path(d, "event.defences.preset", "all")  # creates missing intermediate dicts
    assert get_by_path(d, "attackers.1.request_multiplier") == 100
    assert get_by_path(d, "attackers.0.request_multiplier") == 1
    assert get_by_path(d, "event.defences.preset") == "all"
    assert get_by_path(d, "event.nope.deeper") is None


def test_apply_overrides_does_not_mutate_original():
    base = {"event": {"mode": "LOTTERY"}, "attackers": [{"request_multiplier": 1}]}
    out = apply_overrides(base, {"event.mode": "FCFS", "attackers.0.request_multiplier": 50})
    assert out["event"]["mode"] == "FCFS" and out["attackers"][0]["request_multiplier"] == 50
    assert base == {"event": {"mode": "LOTTERY"}, "attackers": [{"request_multiplier": 1}]}


# --- spec loading and expansion ------------------------------------------------------------

def test_specs_load_with_expected_shape():
    e1 = ExperimentSpec.from_yaml(EXPERIMENTS / "E1.yaml")
    e2 = ExperimentSpec.from_yaml(EXPERIMENTS / "E2.yaml")
    e5 = ExperimentSpec.from_yaml(EXPERIMENTS / "E5.yaml")
    assert e1.kind == "sweep" and len(e1.series) == 3 and e1.x.values == [1, 10, 100, 1000]
    assert e2.kind == "sweep" and len(e2.series) == 3 and len(e2.x.values) == 4
    assert e5.kind == "sweep" and len(e5.series) == 1 and e5.x.values[0] == "none"
    assert e5.baseline is not None and e5.chart_id == "ablation_bot_share"


def test_all_spec_files_load():
    paths = sorted(EXPERIMENTS.glob("E[0-9].yaml"))
    assert len(paths) == 8
    for p in paths:
        assert ExperimentSpec.from_yaml(p).id == p.stem


def _inline_spec(**over) -> ExperimentSpec:
    d = {
        "id": "T", "title": "t", "chart_id": "t_chart", "base": {"name": "b"}, "repeats": 1,
        "x": {"label": "n", "param": "attackers.0.identities", "values": [5, 20]},
        "series": [{"name": "A", "overrides": {"event.defences.preset": "none"}},
                   {"name": "B", "overrides": {"event.defences.preset": "all"}}],
    }
    d.update(over)
    return ExperimentSpec.model_validate(d)


def test_expand_cells_sweep_and_runs():
    cells = expand_cells(_inline_spec())
    assert len(cells) == 4
    assert [(s, x) for s, x, _ in cells] == [("A", 5), ("A", 20), ("B", 5), ("B", 20)]
    assert cells[3][2] == {"event.defences.preset": "all", "attackers.0.identities": 20}

    runs = expand_cells(_inline_spec(kind="runs", x=None))
    assert [(s, x) for s, x, _ in runs] == [("A", "A"), ("B", "B")]


def test_final_experiment_refuses_mock(tmp_path):
    spec = _inline_spec(final=True, base={"name": "b", "target": "mock"})
    with pytest.raises(MockNotAllowed):
        asyncio.run(run_experiment(spec, TargetConfig(base_url="http://127.0.0.1:1"), tmp_path))


# --- chart building -----------------------------------------------------------------------

def _fab_cell(root: Path, series: str, x, bot_seats: int) -> CellResult:
    dirs = [write_run(root / f"{series}_{x}_{i}", seed=10 + i, bot_seats=bot_seats + i, bot_entrants=200,
                      human_entrants=800, human_seats=100 - bot_seats - i, cost_requests=1000)
            for i in range(3)]
    res = build_results(dirs, run_id=f"{series}-{x}", boot_resamples=500, perm_resamples=100)
    return CellResult(series, x, res, dirs)


def test_build_chart_from_fabricated_cells(tmp_path):
    spec = _inline_spec(baseline={"name": "neutral", "metric": "fairness.bot_entrant_share"})
    cells = [_fab_cell(tmp_path, s, x, bs) for s, x, bs in
             [("A", 5, 10), ("A", 20, 30), ("B", 5, 5), ("B", 20, 8)]]
    pts: dict[str, list] = {"A": [], "B": []}
    for c in cells:
        pts[c.series].append(_metric_point(c.results, spec.metric, c.x))

    chart = _build_chart(spec, pts, "mock", cells)
    assert [s.name for s in chart.series] == ["A", "B", "neutral"]
    a = chart.series[0]
    assert [p.x for p in a.points] == [5, 20]
    assert a.points[0].y == pytest.approx(0.11)  # mean bot seats 11/100
    assert a.points[0].ci_low is not None and a.points[0].ci_low <= a.points[0].y <= a.points[0].ci_high
    assert a.points[0].n == 300  # pooled Wilson: total seats across the 3 runs
    base = chart.series[2]
    assert len(base.points) == 2 and all(p.ci_low is None for p in base.points)  # flat reference, one per x
    assert chart.experiment_id == "T" and len(chart.run_ids) == 4

    again = ChartDataset.model_validate(json.loads(json.dumps(chart.to_json())))
    assert again.chart_id == "t_chart" and len(again.series) == 3


# --- live end-to-end (in-process mock) -------------------------------------------------------

def _tiny_spec() -> ExperimentSpec:
    base = {
        "name": "tiny", "target": "mock", "seed": 7, "repeats": 2,
        "event": {"inventory": 10, "window_seconds": 2, "claim_ttl_seconds": 2, "mode": "LOTTERY",
                  "defences": {"preset": "none"}},
        "legit": {"count": 40, "device": {"pow_mode": "modelled_delay", "captcha_solve_s_mean": 0.1, "captcha_fail_rate": 0.0, "hash_rate_lognormal_mu": 18.0}, "poll": {"interval_s_mean": 0.5, "max_polls": 10},
                  "claim": {"delay_lognormal_mu": -1.5, "delay_lognormal_sigma": 0.3, "no_show_rate": 0.0},
                  "retry": {"jitter_ms_mean": 200}},
        "attackers": [{"profile": "sybil_single_ip", "identities": 5, "ips": 1, "rps_per_identity": 1}],
        "load": {"max_in_flight": 100, "procs": 1, "lead_s": 1.0, "claim_phase_s": 2},
    }
    return ExperimentSpec.model_validate({
        "id": "TINY", "title": "Tiny sweep", "chart_id": "tiny_chart", "y_label": "Bot seat share",
        "base": base, "repeats": 2,
        "x": {"label": "Bot identities", "param": "attackers.0.identities", "values": [5, 20], "scale": "linear"},
        "series": [{"name": "no defences", "overrides": {"event.defences.preset": "none"}},
                   {"name": "all defences", "overrides": {"event.defences.preset": "all", "event.defences.layers.pow.difficulty_bits": 8}}],
        "metric": "fairness.bot_seat_share",
    })


@pytest.fixture(scope="module")
def tiny_out(mock_url, tmp_path_factory):  # noqa: F811
    out_root = tmp_path_factory.mktemp("exp")
    out = asyncio.run(run_experiment(_tiny_spec(), TargetConfig(base_url=mock_url), out_root,
                                     allow_mock=True, log=lambda _m: None))
    return out


def test_tiny_experiment_end_to_end(tiny_out):
    out = tiny_out
    assert len(out.cells) == 4 and out.integrity_passed is True
    chart_file = out.out_dir / "tiny_chart.json"
    assert chart_file.exists()
    chart = ChartDataset.model_validate(json.loads(chart_file.read_text(encoding="utf-8")))
    assert [s.name for s in chart.series] == ["no defences", "all defences"]
    assert all(len(s.points) == 2 for s in chart.series)
    result_files = list(out.out_dir.glob("*.results.json"))
    assert len(result_files) == 4
    for f in result_files:
        res = Results.model_validate_json(f.read_text(encoding="utf-8"))
        assert res.repeats == 2


@pytest.mark.skipif(not TSX.exists() or shutil.which("node") is None, reason="frontend node_modules not installed")
def test_experiment_chart_passes_frontend_zod_schema(tiny_out):
    chart_file = tiny_out.out_dir / "tiny_chart.json"
    r = subprocess.run([str(TSX), str(CHART_CHECK), str(chart_file)], capture_output=True, text=True,
                       cwd=REPO / "frontend", timeout=120, shell=sys.platform == "win32")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_undefined_metric_is_skipped_visibly_not_a_crash(tmp_path):
    """A cell whose metric is null (total lockout) yields no chart point, but the skip is
    recorded in the chart notes - never silent."""
    from fairdrop_sim.runner.experiment import ExperimentSpec, SeriesSpec, _build_chart, _metric_point
    from tests.test_metrics_aggregate import build_results, write_lockout_run

    res = build_results([write_lockout_run(tmp_path / "l0", seed=0), write_lockout_run(tmp_path / "l1", seed=1)],
                        boot_resamples=200, perm_resamples=100)
    assert _metric_point(res, "fairness.bot_seat_share", 10) is None
    assert _metric_point(res, "fairness.human_entry_success_rate", 10).y == 0.0

    spec = ExperimentSpec(id="EX", title="t", chart_id="bot_share_vs_identities", base={}, series=[SeriesSpec(name="s")],
                          x=None, kind="runs")
    chart = _build_chart(spec, {"s": []}, "mock", [], ["s x=10: fairness.bot_seat_share undefined"])
    assert chart.series == [] and "NO POINT for" in chart.notes
