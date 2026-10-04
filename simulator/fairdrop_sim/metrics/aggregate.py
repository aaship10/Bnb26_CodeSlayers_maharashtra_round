"""Turn a set of run directories (repeats of one scenario) into a schema_version 1
Results object: every fairness mean a Stat with a 95% CI and n, system/detection as
Stat-or-number, integrity worst-cased, provenance (target, synthetic) carried through.

Seeds for the bootstrap come from the scenario seed so a Results file is reproducible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.metrics import detection as D
from fairdrop_sim.metrics import fairness as F
from fairdrop_sim.metrics import stats as S
from fairdrop_sim.metrics import system as SY
from fairdrop_sim.metrics.integrity import integrity_across_runs
from fairdrop_sim.metrics.loader import RunData, load_run
from fairdrop_sim.models.results import (
    AttackerCostPerSeat,
    Detection,
    EventSummary,
    Fairness,
    Metrics,
    Population,
    PerRun,
    Results,
    SystemMetrics,
)
from fairdrop_sim.seeds import derive_seed

COST_FIELDS = ("requests", "accounts", "pow_hashes", "captcha_solves", "usd_modelled")


def _boot_seed(master: int, label: str) -> int:
    return derive_seed(master, "metrics", label)


def build_results(run_dirs: list[str | Path], *, run_id: str | None = None,
                  scenario_id: str | None = None, boot_resamples: int = 10_000,
                  perm_resamples: int = 10_000) -> Results:
    runs: list[RunData] = [load_run(d) for d in run_dirs]
    if not runs:
        raise ValueError("no runs to aggregate")
    meta0 = runs[0].meta
    cfg = meta0.get("scenario_config", {})
    master = int(cfg.get("seed", meta0.get("run_seed", 0)))
    target = meta0["target"]
    synthetic = bool(meta0.get("synthetic", False))

    per_run_fair: list[dict] = []
    order_parts, won_parts = [], []
    err_rows: dict[str, list[float]] = {k: [] for k in ("http_429_legit", "http_429_bot", "http_5xx", "timeout")}
    thr, avail = [], []
    det_rows: dict[str, list[float]] = {k: [] for k in
                                        ("precision", "recall", "false_positive_rate", "false_positive_rate_nat")}
    cost_num: dict[str, list[float]] = {k: [] for k in COST_FIELDS}
    cost_den: list[float] = []
    per_run_models: list[PerRun] = []
    merged_rec = Recorder(0.0)

    for i, r in enumerate(runs):
        humans = r.meta.get("population", {}).get("legit")
        fr = F.fairness_per_run(r.users, humans_intended=humans)
        per_run_fair.append(fr)
        order_parts.append(fr.pop("_corr_order"))
        won_parts.append(fr.pop("_corr_won"))

        er = SY.error_rates_per_run(r.recorder)
        for k in err_rows:
            err_rows[k].append(er[k])
        t = SY.throughput_per_run(r.recorder)
        if t is not None:
            thr.append(t)
        a = r.meta.get("load", {}).get("availability_spike")
        if a is not None:
            avail.append(a)

        det = D.detection_per_run(r.users, D.rejected_from_decisions(r.decisions))
        for k in det_rows:
            if det.get(k) is not None:
                det_rows[k].append(det[k])

        seats = 0
        totals = {k: 0.0 for k in COST_FIELDS}
        for atk in r.meta.get("attackers", []):
            seats += atk.get("seats_won", 0)
            tot = atk.get("cost", {}).get("totals", {})
            for k in COST_FIELDS:
                totals[k] += float(tot.get(k, 0) or 0)
        cost_den.append(seats)
        for k in COST_FIELDS:
            cost_num[k].append(totals[k])

        merged_rec.merge(r.recorder.to_dict())
        per_run_models.append(PerRun(
            index=i, seed=int(r.meta.get("run_seed", 0)),
            status="ok" if r.meta.get("integrity", {}).get("passed") else "failed",
            values={
                "bot_seat_share": fr["bot_seat_share"],
                "human_win_prob": fr["human_win_prob"],
                "bot_final_seats": fr["bot_final_seats"],
                "integrity_passed": bool(r.meta.get("integrity", {}).get("passed")),
            },
        ))

    def fstat(key: str):
        return S.forced_stat([f[key] for f in per_run_fair], boot_resamples, _boot_seed(master, key))

    win_counts = F.win_counts_by_identity([r.users for r in runs], label=F.HUMAN)
    jain = S.jain_index(win_counts)
    gini = S.gini(win_counts)
    inv = cfg.get("event", {}).get("inventory", 0)
    # CI by bootstrapping over identities (n = population size), not over runs.
    jain_stat = S.bootstrap_stat(win_counts, S.jain_index, boot_resamples, _boot_seed(master, "jain"))
    gini_stat = S.bootstrap_stat(win_counts, S.gini, boot_resamples, _boot_seed(master, "gini"))

    cost = AttackerCostPerSeat(**{
        k: S.ratio_stat(cost_num[k], cost_den, boot_resamples, _boot_seed(master, f"cost_{k}"))
        for k in COST_FIELDS
    })

    def pooled(num_key: str, den_key: str) -> "S.Stat | None":
        """Wilson interval on summed counts across runs (brief: Wilson for proportions).
        Sound at any number of repeats, unlike a bootstrap over a handful of run means."""
        num = sum(int(f[num_key]) for f in per_run_fair)
        den = sum(int(f[den_key]) for f in per_run_fair)
        return S.proportion_stat(num, den) if den > 0 else None

    fairness = Fairness(
        bot_seat_share=pooled("bot_final_seats", "all_final_seats") or fstat("bot_seat_share"),
        bot_entrant_share=pooled("bot_entrants", "all_entrants") or fstat("bot_entrant_share"),
        human_win_prob=pooled("_human_seats", "_humans_intended") or fstat("human_win_prob"),
        human_entry_success_rate=pooled("_human_entrants", "_humans_intended") or fstat("human_entry_success_rate"),
        arrival_time_correlation=fstat("arrival_time_correlation"),
        arrival_time_perm_p=F.pooled_arrival_perm_p(order_parts, won_parts, perm_resamples,
                                                    _boot_seed(master, "perm")),
        jain_index=jain_stat,
        gini=gini_stat,
        attacker_cost_per_seat=cost,
    )

    def vstat(vals, label):
        return S.value_or_stat(vals, boot_resamples, _boot_seed(master, label)) if vals else None

    from fairdrop_sim.models.results import ErrorRates

    lag_hist = merged_rec.sched_lag
    lag_pcts = None
    if lag_hist.get_total_count():
        from fairdrop_sim.models.results import Percentiles
        ms = lambda q: round(lag_hist.get_value_at_percentile(q) / 1000, 3)  # noqa: E731
        lag_pcts = Percentiles(p50=ms(50), p95=ms(95), p99=ms(99), n=lag_hist.get_total_count())

    system = SystemMetrics(
        latency_ms=SY.latency_block(merged_rec),
        scheduler_lag_ms=lag_pcts,
        throughput_rps=vstat(thr, "thr"),
        error_rates=ErrorRates(
            http_429_legit=vstat(err_rows["http_429_legit"], "429l"),
            http_429_bot=vstat(err_rows["http_429_bot"], "429b"),
            http_5xx=vstat(err_rows["http_5xx"], "5xx"),
            timeout=vstat(err_rows["timeout"], "to"),
        ),
        availability=vstat(avail, "avail"),
    )
    det = Detection(
        precision=vstat(det_rows["precision"], "prec"),
        recall=vstat(det_rows["recall"], "rec"),
        false_positive_rate=vstat(det_rows["false_positive_rate"], "fpr"),
        false_positive_rate_nat=vstat(det_rows["false_positive_rate_nat"], "fprn"),
    )
    integ = integrity_across_runs([r.meta.get("integrity", {}) for r in runs])

    pop = meta0.get("population", {})
    notes = []
    if target == "mock":
        notes.append("MOCK target: development double, not evidence about the real system.")
    if jain is not None:
        # Reference must use the seats humans ACTUALLY win per run (not the full inventory):
        # bots take some seats, so human seats/run < inventory, and a reference built from
        # the inventory would sit above the observed value and read as "fairer than fair".
        human_seats = [max(0, f["all_final_seats"] - f["bot_final_seats"]) for f in per_run_fair]
        mean_human_seats = int(round(sum(human_seats) / len(human_seats))) if human_seats else 0
        ref = S.fair_lottery_jain(len(win_counts), mean_human_seats, len(runs))
        if ref is not None:
            notes.append(f"Fair-lottery reference Jain index (humans, {mean_human_seats} seats/run) ≈ {ref:.3f}; "
                         "sampling variance alone keeps a fair lottery below 1. A value near this reference is fair; "
                         "well above it would mean wins concentrate on fewer identities.")

    if lag_pcts is not None and lag_pcts.p99 is not None and lag_pcts.p99 > 50:
        notes.append(f"Load-generator scheduler lag p99 = {lag_pcts.p99} ms (> 50 ms): the open-loop latencies "
                     "above include client-side queueing, not only server time. Do not quote these as server latency.")

    return Results(
        run_id=run_id or f"agg-{cfg.get('name', 'run')}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
        scenario_id=scenario_id or cfg.get("name", meta0.get("scenario", "unknown")),
        target=target,
        synthetic=synthetic,
        seed=master,
        repeats=len(runs),
        failed_runs=sum(1 for p in per_run_models if p.status == "failed"),
        created_at=datetime.now(timezone.utc),
        population=Population(legit=pop.get("legit", 0), bots=pop.get("bots", 0),
                             bot_identities=pop.get("bot_identities", 0)),
        event=EventSummary(inventory=inv, mode=cfg.get("event", {}).get("mode", "LOTTERY"),
                           defences=cfg.get("event", {}).get("defences", {})),
        metrics=Metrics(fairness=fairness, system=system, detection=det, integrity=integ),
        per_run=per_run_models,
        notes=notes,
        scenario=cfg or None,
    )
