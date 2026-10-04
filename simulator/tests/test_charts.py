"""Chart rendering rules, the latency / multi-metric chart builders, and the bot-timing fix."""
from __future__ import annotations

import io
import random

import numpy as np
import pytest
from matplotlib import image as mpimg

from fairdrop_sim.charts.render import (
    CRITICAL,
    SLOTS,
    TooManySeries,
    assign_slots,
    render_png,
    watermark_text,
)
from fairdrop_sim.models import ChartDataset, Point, Series
from fairdrop_sim.samples import sample_charts


def chart(series: list[Series], *, target="mock", synthetic=False, x_scale="log", title="t") -> ChartDataset:
    return ChartDataset(chart_id="bot_share_vs_identities", title=title, x_label="x", y_label="Bot seat share",
                        x_scale=x_scale, series=series, target=target, synthetic=synthetic)


def pts(ys, xs=(1, 10, 100)):
    return [Point(x=x, y=y, ci_low=y * 0.8, ci_high=y * 1.2, n=10) for x, y in zip(xs, ys)]


def pixels(png: bytes) -> np.ndarray:
    return mpimg.imread(io.BytesIO(png), format="png")


def count_color(img: np.ndarray, hex_color: str, region=(slice(None), slice(None)), tol=0.06) -> int:
    rgb = np.array([int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]) / 255
    sub = img[region][..., :3]
    return int((np.abs(sub - rgb).max(axis=-1) < tol).sum())


# --------------------------------------------------------------------------- colour rules


def test_colour_follows_the_entity_not_its_rank():
    """FCFS keeps its slot whether it is listed first or last, and survivors of a filter keep theirs."""
    a = assign_slots(["FCFS", "Fair Drop, no defences", "Fair Drop, all defences"])
    b = assign_slots(["Fair Drop, all defences", "Fair Drop, no defences", "FCFS"])
    assert a == b and a["FCFS"] == 2 and a["Fair Drop, no defences"] == 1 and a["Fair Drop, all defences"] == 3
    only = assign_slots(["Fair Drop, all defences"])  # filtered down to one series: same colour
    assert only["Fair Drop, all defences"] == a["Fair Drop, all defences"]


def test_slots_are_unique_and_baselines_get_none():
    s = assign_slots(["none", "rate_limit", "rate_limit+pow", "all", "Lottery-neutral baseline"])
    assert "Lottery-neutral baseline" not in s
    assert len(set(s.values())) == len(s) == 4
    assert all(1 <= v <= 8 for v in s.values())


def test_a_ninth_series_is_refused_not_invented():
    with pytest.raises(TooManySeries):
        assign_slots([f"series {i}" for i in range(9)])
    assert len(assign_slots([f"series {i}" for i in range(8)])) == 8  # eight is the cap, and it works


def test_watermark_rules():
    s = Series(name="a", points=pts([0.1, 0.2, 0.3]))
    assert watermark_text(chart([s], target="mock")) == "MOCK DATA"
    assert watermark_text(chart([s], target="real", synthetic=True)) == "SYNTHETIC DATA"
    assert watermark_text(chart([s], target="real", synthetic=False)) is None


# --------------------------------------------------------------------------- rendering


def test_every_sample_chart_renders_to_a_real_png():
    for c in sample_charts():
        png = render_png(c)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        img = pixels(png)
        assert img.shape[0] == 800 and img.shape[1] == 1280  # 8x5 in at 160 dpi


def test_mock_and_synthetic_are_marked_but_real_is_not():
    s = [Series(name="A", points=pts([0.1, 0.2, 0.3])), Series(name="B", points=pts([0.3, 0.2, 0.1]))]
    badge = (slice(20, 80), slice(1000, 1270))  # the top-right corner badge
    mock = pixels(render_png(chart(s, target="mock")))
    real = pixels(render_png(chart(s, target="real", synthetic=False)))
    assert count_color(mock, CRITICAL, badge) > 100  # red badge text/outline present
    assert count_color(real, CRITICAL, badge) == 0  # a real result carries no mock marking
    assert not np.array_equal(mock, real)


def test_series_are_drawn_in_their_slot_colours():
    s = [Series(name="FCFS", points=pts([0.2, 0.5, 0.9])),
         Series(name="Fair Drop, no defences", points=pts([0.02, 0.02, 0.02]))]
    img = pixels(render_png(chart(s, target="real")))
    plot = (slice(130, 650), slice(100, 1000))
    assert count_color(img, SLOTS[2], plot) > 300  # FCFS orange
    assert count_color(img, SLOTS[1], plot) > 300  # no-defences blue


def test_category_axis_and_missing_ci_render():
    s = [Series(name="Precision", points=[Point(x="p50", y=12.0), Point(x="p95", y=60.0), Point(x="p99", y=140.0)]),
         Series(name="Other", points=[Point(x="p50", y=20.0), Point(x="p95", y=90.0), Point(x="p99", y=200.0)])]
    c = ChartDataset(chart_id="latency_percentiles_normal_vs_attack", title="lat", x_label="Percentile",
                     y_label="Latency (ms)", x_scale="category", series=s, target="real", synthetic=False)
    assert render_png(c)[:4] == b"\x89PNG"


# --------------------------------------------------------------------------- chart builders


def _results(tmp_path, name: str, **kw):
    from tests.test_metrics_aggregate import build_results, write_run

    base = dict(seed=1, bot_seats=20, bot_entrants=200, human_entrants=800, human_seats=80, cost_requests=1000)
    base.update(kw)
    return build_results([write_run(tmp_path / name, **base)], boot_resamples=200, perm_resamples=100)


def test_latency_chart_uses_pooled_percentiles(tmp_path):
    from fairdrop_sim.runner.experiment import CellResult, ExperimentSpec, SeriesSpec, _latency_chart

    spec = ExperimentSpec(id="E3", title="Latency", chart_id="latency_percentiles_normal_vs_attack", kind="runs",
                          chart_kind="latency", base={}, series=[SeriesSpec(name="Normal"), SeriesSpec(name="Attack")])
    cells = [CellResult("Normal", "Normal", _results(tmp_path, "n"), []),
             CellResult("Attack", "Attack", _results(tmp_path, "a", seed=2), [])]
    c = _latency_chart(spec, "mock", cells)
    assert [s.name for s in c.series] == ["Normal", "Attack"]
    for s in c.series:
        assert [p.x for p in s.points] == ["p50", "p95", "p99"]
        assert s.points[0].y <= s.points[1].y <= s.points[2].y
        assert all(p.ci_low is None for p in s.points)  # one merged histogram: no CI is claimed
    assert "no CI" in c.notes and c.x_scale == "category"
    spec2 = spec.model_copy(update={"kind": "sweep"})
    with pytest.raises(ValueError, match="kind 'runs'"):
        _latency_chart(spec2, "mock", cells)


def test_multi_metric_chart_one_series_per_metric(tmp_path):
    from fairdrop_sim.runner.experiment import (
        AxisSpec, CellResult, ExperimentSpec, MetricSeries, SeriesSpec, _multi_metric_chart)

    spec = ExperimentSpec(
        id="E4", title="Detection", chart_id="detection_precision_recall_by_layer", chart_kind="multi_metric",
        base={}, series=[SeriesSpec(name="Botnet")], x=AxisSpec(label="preset", param="p", values=["a", "b"], scale="category"),
        metrics=[MetricSeries(name="Precision", metric="fairness.bot_seat_share"),
                 MetricSeries(name="Recall", metric="detection.recall")])
    cells = [CellResult("Botnet", "a", _results(tmp_path, "a"), []), CellResult("Botnet", "b", _results(tmp_path, "b", seed=2), [])]
    skipped: list[str] = []
    c = _multi_metric_chart(spec, "mock", cells, skipped)
    assert [s.name for s in c.series] == ["Precision"]  # detection.recall is null here (no defence ran)
    assert len(c.series[0].points) == 2
    assert len(skipped) == 2 and "NO POINT for" in c.notes  # the undefined metric is reported, not dropped


# --------------------------------------------------------------------------- bot timing (E1 realism)


def _ctx(rps: float, multiplier: float, profile="retry_spammer"):
    from fairdrop_sim.bots.base import BotContext
    from fairdrop_sim.bots.cost import CostAccount
    from fairdrop_sim.bots.profiles import behavior_for
    from fairdrop_sim.challenge.solver import Solver
    from fairdrop_sim.models.scenario import AttackerConfig

    a = AttackerConfig(profile=profile, identities=1, rps_per_identity=rps, request_multiplier=multiplier)
    return BotContext(api=None, attacker=a, behavior=behavior_for(profile), attacker_index=0, run_seed=1, run_tag="t",
                      mode="FCFS", t_open=1000.0, t_close=1006.0, t_draw=1007.0, t_end=1012.0, solver=Solver(),
                      cost=CostAccount())


def test_higher_request_rate_means_an_earlier_first_hit():
    """A bot polling at a given rate has an unknown phase, so its first attempt lands uniformly in
    the first interval. That is what makes request volume matter under FCFS (and only there)."""
    from fairdrop_sim.bots.base import _enter_schedule

    first = lambda ctx: [(_enter_schedule(ctx, random.Random(i))[0] - ctx.t_open) for i in range(300)]  # noqa: E731
    slow, fast = first(_ctx(0.5, 1)), first(_ctx(0.5, 1000))
    assert 0.8 < np.mean(slow) < 1.2 and max(slow) <= 2.0  # interval = 2 s => mean ~1 s
    assert np.mean(fast) < 0.002  # interval = 2 ms => essentially at the opening
    assert np.mean(fast) < np.mean(slow) / 100


def test_speed_bot_still_fires_exactly_at_open():
    from fairdrop_sim.bots.base import _enter_schedule

    ctx = _ctx(1.0, 1, profile="speed_bot")
    assert _enter_schedule(ctx, random.Random(3))[0] == ctx.t_open
