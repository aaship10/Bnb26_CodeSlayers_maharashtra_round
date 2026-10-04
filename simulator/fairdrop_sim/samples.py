"""SYNTHETIC sample results and chart datasets for D's dashboard (docs/sample_results/).

These numbers are FAKE. They come from back-of-envelope formulas plus seeded
noise so the UI has realistic shapes, CIs and edge cases (null CIs, null
detection, FCFS without a draw) to render. Every file carries synthetic=true,
target="mock" and a run_id starting with "synthetic-". Never cite them as findings.

    fdsim samples            # regenerates docs/sample_results deterministically
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from fairdrop_sim.metrics.stats import mean_stat
from fairdrop_sim.models import SCHEMA_MODELS, ChartDataset, Experiment, Point, Results, Series, Stat
from fairdrop_sim.models.results import (
    AttackerCostPerSeat,
    Detection,
    ErrorRates,
    EventSummary,
    Fairness,
    Integrity,
    Metrics,
    Percentiles,
    PerRun,
    Population,
    SystemMetrics,
)
from fairdrop_sim.seeds import derive_seed

CREATED_AT = datetime(2026, 10, 3, tzinfo=timezone.utc)
SYNTHETIC_NOTE = "SYNTHETIC: fake numbers for UI development. Not a measurement. Do not cite."
MASTER_SEED = 7  # samples only
BOOT = 2_000  # bootstrap resamples for samples (real results use 10,000)


def _stat(values: np.ndarray | list[float], *labels: object) -> Stat:
    return mean_stat(list(values), resamples=BOOT, seed=derive_seed(MASTER_SEED, "boot", *labels))


def _rng(*labels: object) -> np.random.Generator:
    return np.random.default_rng(derive_seed(MASTER_SEED, *labels))


# --------------------------------------------------------------------------- results


def _demo_results(run_id: str, title: str, *, mode: str, preset: str, bot_ids: int, bot_weight: float,
                  profile: str, repeats: int = 30, legit: int = 2000, inventory: int = 100,
                  bot_requests: float, detection: bool) -> Results:
    rng = _rng(run_id)
    humans_ok = legit
    lockout = 0.015 if preset == "all" else 0.0
    per_run: list[PerRun] = []
    rows: dict[str, list[float]] = {k: [] for k in (
        "bot_share", "entrant_share", "human_win", "entry_ok", "corr", "req_per_seat", "acct_per_seat",
        "hash_per_seat", "thr", "429l", "429b", "5xx", "to", "prec", "rec", "fpr", "fpr_nat", "avail")}
    for i in range(repeats):
        entered_h = rng.binomial(humans_ok, 1 - lockout - 0.002)
        if mode == "FCFS":
            bot_seats = bot_ids  # speed wins: every bot identity lands a seat
        else:
            p = bot_ids * bot_weight / (bot_ids * bot_weight + entered_h)
            bot_seats = int(rng.binomial(inventory, p))
        seats_h = inventory - bot_seats
        rows["bot_share"].append(bot_seats / inventory)
        rows["entrant_share"].append(bot_ids / (bot_ids + entered_h))
        rows["human_win"].append(seats_h / humans_ok)
        rows["entry_ok"].append(entered_h / humans_ok)
        rows["corr"].append(rng.normal(-0.41, 0.03) if mode == "FCFS" else rng.normal(0, 0.022))
        rows["req_per_seat"].append(bot_requests * rng.uniform(0.95, 1.05) / max(bot_seats, 1))
        rows["acct_per_seat"].append(bot_ids / max(bot_seats, 1))
        rows["hash_per_seat"].append(bot_ids * 2**16 * rng.uniform(0.9, 1.1) / max(bot_seats, 1))
        rows["thr"].append(rng.normal(1450 if bot_requests > 1e4 else 620, 40))
        rows["429l"].append(max(0.0, rng.normal(0.012, 0.003)) if preset == "all" else 0.0)
        rows["429b"].append(min(1.0, rng.normal(0.64, 0.04)) if preset == "all" else 0.0)
        rows["5xx"].append(max(0.0, rng.normal(0.0004, 0.0002)))
        rows["to"].append(max(0.0, rng.normal(0.001, 0.0005)))
        rows["avail"].append(1 - rows["5xx"][-1] - rows["to"][-1])
        rows["prec"].append(min(1.0, rng.normal(0.965, 0.015)))
        rows["rec"].append(min(1.0, rng.normal(0.9, 0.03)))
        rows["fpr"].append(max(0.0, rng.normal(0.004, 0.0015)))
        rows["fpr_nat"].append(max(0.0, rng.normal(0.06, 0.015)))
        per_run.append(PerRun(index=i, seed=derive_seed(MASTER_SEED, run_id, "run", i), status="ok", values={
            "bot_seat_share": rows["bot_share"][-1],
            "human_win_prob": rows["human_win"][-1],
            "bot_seats": bot_seats,
            "integrity_passed": True,
        }))
    s = lambda k: _stat(rows[k], run_id, k)  # noqa: E731
    lat = {
        "enter": Percentiles(p50=18.0 if mode == "FCFS" else 14.0, p95=86.0, p99=212.0, n=repeats * 2600),
        "status": Percentiles(p50=6.0, p95=31.0, p99=77.0, n=repeats * 9100),
        "claim": Percentiles(p50=11.0, p95=40.0, p99=95.0, n=repeats * inventory),
        "enter.legit": Percentiles(p50=15.0, p95=80.0, p99=190.0, n=repeats * 2500),
        "enter.bot": Percentiles(p50=21.0, p95=95.0, p99=240.0, n=repeats * 100),
    }
    fairness = Fairness(
        bot_seat_share=s("bot_share"),
        bot_entrant_share=s("entrant_share"),
        human_win_prob=s("human_win"),
        human_entry_success_rate=s("entry_ok"),
        arrival_time_correlation=s("corr"),
        arrival_time_perm_p=0.0001 if mode == "FCFS" else 0.47,
        jain_index=Stat(mean=0.049, ci_low=0.047, ci_high=0.051, n=legit) if mode == "FCFS"
        else Stat(mean=0.61, ci_low=0.58, ci_high=0.64, n=legit),
        gini=Stat(mean=0.95, ci_low=0.94, ci_high=0.96, n=legit) if mode == "FCFS"
        else Stat(mean=0.43, ci_low=0.41, ci_high=0.45, n=legit),
        attacker_cost_per_seat=AttackerCostPerSeat(
            requests=s("req_per_seat"),
            accounts=s("acct_per_seat"),
            pow_hashes=s("hash_per_seat") if preset in ("rate_limit+pow", "all") else None,
        ),
    )
    detection_m = Detection(
        precision=s("prec"), recall=s("rec"), false_positive_rate=s("fpr"), false_positive_rate_nat=s("fpr_nat"),
    ) if detection else Detection()
    return Results(
        run_id=run_id,
        scenario_id=run_id.removeprefix("synthetic-"),
        target="mock",
        synthetic=True,
        seed=MASTER_SEED,
        repeats=repeats,
        failed_runs=0,
        created_at=CREATED_AT,
        population=Population(legit=legit, bots=1, bot_identities=bot_ids),
        event=EventSummary(inventory=inventory, mode=mode, defences={"preset": preset}),
        metrics=Metrics(
            fairness=fairness,
            system=SystemMetrics(
                latency_ms=lat,
                throughput_rps=s("thr"),
                error_rates=ErrorRates(http_429_legit=s("429l"), http_429_bot=s("429b"), http_5xx=s("5xx"),
                                       timeout=s("to")),
                availability=s("avail"),
            ),
            detection=detection_m,
            integrity=Integrity(draw_verified=None if mode == "FCFS" else True, passed=True),
        ),
        per_run=per_run,
        notes=[SYNTHETIC_NOTE, title, f"attack profile: {profile}"],
    )


def sample_results() -> list[Results]:
    return [
        _demo_results("synthetic-demo-fcfs-flood", "FCFS under a 20-identity speed-bot flood", mode="FCFS",
                      preset="none", bot_ids=20, bot_weight=1.0, profile="speed_bot", bot_requests=50_000,
                      detection=False),
        _demo_results("synthetic-demo-lottery-flood", "Fair Drop (lottery, no defences) under the same flood",
                      mode="LOTTERY", preset="none", bot_ids=20, bot_weight=1.0, profile="speed_bot",
                      bot_requests=50_000, detection=False),
        _demo_results("synthetic-demo-sybil200-off", "Sybil, 200 identities, defences off", mode="LOTTERY",
                      preset="none", bot_ids=200, bot_weight=1.0, profile="sybil_single_ip", bot_requests=2_000,
                      detection=False),
        _demo_results("synthetic-demo-sybil200-on", "Sybil, 200 identities, all defences on", mode="LOTTERY",
                      preset="all", bot_ids=200, bot_weight=0.25, profile="sybil_single_ip", bot_requests=2_000,
                      detection=True),
    ]


# --------------------------------------------------------------------------- charts


def _pt(x: float | str, vals: np.ndarray, *labels: object) -> Point:
    st = _stat(vals, *labels)
    return Point(x=x, y=st.mean, ci_low=st.ci_low, ci_high=st.ci_high, n=st.n)


def _chart(chart_id: str, exp: str, title: str, x_label: str, y_label: str, series: list[Series], *,
           x_scale: str = "linear", y_scale: str = "linear", notes: str = "") -> ChartDataset:
    return ChartDataset(chart_id=chart_id, title=title, x_label=x_label, y_label=y_label, x_scale=x_scale,
                        y_scale=y_scale, series=series, notes=f"{SYNTHETIC_NOTE} {notes}".strip(), target="mock",
                        synthetic=True, experiment_id=exp, run_ids=[f"synthetic-{exp}"])


HUMANS, SEATS, REPS = 50_000, 500, 10


def _lottery_share(k: float, w: float, rng: np.random.Generator, n: int = REPS) -> np.ndarray:
    p = k * w / (k * w + HUMANS)
    return rng.binomial(SEATS, p, size=n) / SEATS


def sample_charts() -> list[ChartDataset]:
    charts: list[ChartDataset] = []

    # E1: request multiplier, 1,000 speed-bot identities (more than the 500 seats, so
    # FCFS can approach 100%; with fewer identities than seats FCFS caps at identities/seats).
    mult = [1, 10, 100, 1000]
    k = 1000
    rng = _rng("E1")
    fcfs_mean = {1: 0.03, 10: 0.31, 100: 0.86, 1000: 0.985}
    s_fcfs = Series(name="FCFS", points=[
        _pt(m, np.clip(rng.normal(fcfs_mean[m], max(0.004, (1 - fcfs_mean[m]) * 0.3), REPS), 0, 1), "E1", "fcfs", m)
        for m in mult])
    s_none = Series(name="Fair Drop, no defences", points=[
        _pt(m, _lottery_share(k, 1.0, rng), "E1", "none", m) for m in mult])
    s_all = Series(name="Fair Drop, all defences", points=[
        _pt(m, _lottery_share(k, 0.5, rng), "E1", "all", m) for m in mult])
    s_base = Series(name="Lottery-neutral baseline (bot entrant share)",
                    points=[Point(x=m, y=k / (k + HUMANS)) for m in mult])
    charts.append(_chart("bot_share_vs_request_multiplier", "E1", "Bot seat share vs request volume",
                         "Bot request multiplier (x human rate)", "Bot seat share", [s_fcfs, s_none, s_all, s_base],
                         x_scale="log", notes="50k humans, 500 seats, 1,000 speed-bot identities, 10 runs per point."))

    # E2 + E6: Sybil sweep.
    ids = [1, 10, 100, 1000, 5000]
    eff = {"none": 1.0, "rate_limit": 0.97, "rate_limit+pow": 0.93, "rate_limit+pow+captcha": 0.78, "all": 0.31}
    rng = _rng("E2")
    shares = {p: {n: _lottery_share(n, w, rng) for n in ids} for p, w in eff.items()}
    charts.append(_chart(
        "bot_share_vs_identities", "E2", "Bot seat share vs number of bot identities (Sybil)",
        "Bot identities", "Bot seat share",
        [Series(name=p, points=[_pt(n, shares[p][n], "E2", p, n) for n in ids]) for p in eff]
        + [Series(name="Lottery-neutral baseline", points=[Point(x=n, y=n / (n + HUMANS)) for n in ids])],
        x_scale="log", notes="distributed_botnet, ips = identities/10, 10 runs per point."))
    charts.append(_chart(
        "human_win_prob_under_attack", "E6", "Human win probability under Sybil attack",
        "Bot identities", "Human win probability",
        [Series(name=p, points=[_pt(n, (1 - shares[p][n]) * SEATS / HUMANS, "E6", p, n) for n in ids])
         for p in ("none", "rate_limit+pow", "all")]
        + [Series(name="No attack", points=[Point(x=n, y=SEATS / HUMANS) for n in ids])],
        x_scale="log", notes="Degradation = no-attack value minus under-attack value."))

    # E3: latency.
    rng = _rng("E3")
    base = {"p50": 12.0, "p95": 64.0, "p99": 150.0}
    att = {"p50": 19.0, "p95": 118.0, "p99": 340.0}
    lat_series = []
    for ep, scale in (("enter", 1.0), ("status", 0.45)):
        for label, ref in (("normal", base), ("under attack", att)):
            lat_series.append(Series(name=f"{ep}, {label}", points=[
                _pt(q, rng.normal(ref[q] * scale, ref[q] * scale * 0.07, REPS), "E3", ep, label, q)
                for q in ("p50", "p95", "p99")]))
    charts.append(_chart("latency_percentiles_normal_vs_attack", "E3", "Latency, normal vs attack load",
                         "Percentile", "Latency (ms, open-loop, from intended send time)", lat_series,
                         x_scale="category", notes="50k humans; attack = all 9 profiles at default knobs."))

    # E4: detection by layer.
    rng = _rng("E4")
    layers = ["rate_limit", "pow", "captcha", "signals", "risk", "all"]
    prec = {"rate_limit": 0.71, "pow": 0.55, "captcha": 0.62, "signals": 0.83, "risk": 0.94, "all": 0.96}
    rec = {"rate_limit": 0.38, "pow": 0.12, "captcha": 0.41, "signals": 0.57, "risk": 0.81, "all": 0.90}
    fpr = {"rate_limit": 0.031, "pow": 0.002, "captcha": 0.05, "signals": 0.012, "risk": 0.006, "all": 0.008}
    det = []
    for name, ref in (("precision", prec), ("recall", rec), ("false positive rate", fpr)):
        det.append(Series(name=name, points=[
            _pt(layer, np.clip(rng.normal(ref[layer], max(ref[layer] * 0.05, 0.002), REPS), 0, 1), "E4", name, layer)
            for layer in layers]))
    charts.append(_chart("detection_precision_recall_by_layer", "E4", "Detection quality per defence layer",
                         "Defence layer (alone)", "Rate", det, x_scale="category",
                         notes="Ground truth from simulator-side labels; FPR is over legitimate users."))

    # E5: ablation.
    rng = _rng("E5")
    cells = ["none", "rate_limit", "pow", "captcha", "signals", "risk", "rate_limit+pow", "rate_limit+pow+captcha",
             "all"]
    abl = {"none": 1.0, "rate_limit": 0.96, "pow": 0.9, "captcha": 0.72, "signals": 0.8, "risk": 0.42,
           "rate_limit+pow": 0.87, "rate_limit+pow+captcha": 0.66, "all": 0.29}
    kk = 2000
    charts.append(_chart(
        "ablation_bot_share", "E5", "Ablation: bot seat share by defence configuration", "Defence configuration",
        "Bot seat share",
        [Series(name="Bot seat share", points=[_pt(c, _lottery_share(kk, abl[c], rng), "E5", c) for c in cells]),
         Series(name="Lottery-neutral baseline", points=[Point(x=c, y=kk / (kk + HUMANS)) for c in cells])],
        x_scale="category", notes="Mixed attack, all 9 profiles, 2,000 bot identities in total."))
    return charts


def sample_experiments(results: list[Results]) -> list[Experiment]:
    titles = {
        "E1": ("Request-multiplier sweep", "Does sending more requests buy more seats? FCFS vs Fair Drop."),
        "E2": ("Sybil sweep", "Bot seat share as identity count grows, per defence preset."),
        "E3": ("Latency under attack", "p50/p95/p99 per endpoint, normal vs attack load."),
        "E4": ("Detection per layer", "Precision, recall and false positives of each defence layer."),
        "E5": ("Ablation", "Each defence layer alone and cumulative, all attack profiles."),
        "E6": ("Human win probability", "How gracefully humans' chances degrade under attack."),
    }
    charts = {c.experiment_id: c.chart_id for c in sample_charts()}
    out = [Experiment(id=f"synthetic-{e}", title=t, description=f"{d} {SYNTHETIC_NOTE}", charts=[charts[e]],
                      run_ids=[f"synthetic-{e}"], target="mock", synthetic=True, created_at=CREATED_AT)
           for e, (t, d) in titles.items()]
    out.append(Experiment(id="synthetic-demos", title="Demo presets",
                          description=f"The three demo presets as single result files. {SYNTHETIC_NOTE}", charts=[],
                          run_ids=[r.run_id for r in results], target="mock", synthetic=True, created_at=CREATED_AT))
    return out


# --------------------------------------------------------------------------- writing


def _dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_schemas(out: Path) -> list[Path]:
    paths = []
    for name, model in SCHEMA_MODELS.items():
        p = out / f"{name}.schema.json"
        _dump(p, model.model_json_schema())
        paths.append(p)
    return paths


def write_samples(out: Path) -> list[Path]:
    results = sample_results()
    written: list[Path] = []
    for r in results:
        p = out / "results" / f"{r.run_id}.json"
        _dump(p, r.model_dump(mode="json"))
        written.append(p)
    for c in sample_charts():
        p = out / "charts" / f"{c.chart_id}.json"
        _dump(p, c.to_json())
        written.append(p)
    p = out / "experiments.json"
    _dump(p, [e.model_dump(mode="json") for e in sample_experiments(results)])
    written.append(p)
    written += write_schemas(out / "schema")
    return written
