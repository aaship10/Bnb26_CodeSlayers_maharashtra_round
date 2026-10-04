"""Metric functions tested on hand-built outcomes with KNOWN answers (brief §9)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fairdrop_sim.metrics import detection as D
from fairdrop_sim.metrics import fairness as F
from fairdrop_sim.metrics import stats as S


def make_users(rows: list[dict]) -> pd.DataFrame:
    cols = ["user_id", "label", "nat_group", "srv_state", "srv_draw_state", "srv_arrival_seq", "srv_weight"]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df


def population(n_human_seats, n_bot_seats, n_human_entrants, n_bot_entrants, n_human_absent=0):
    """A fixed, known population. Seated rows are CLAIMED; other entrants WAITLISTED."""
    rows = []
    seq = 1
    for i in range(n_human_entrants):
        state = "CLAIMED" if i < n_human_seats else "WAITLISTED"
        draw = "WON" if i < n_human_seats else "WAITLISTED"
        rows.append({"user_id": f"h{i}", "label": "legit", "nat_group": -1, "srv_state": state,
                     "srv_draw_state": draw, "srv_arrival_seq": seq, "srv_weight": 1.0})
        seq += 1
    for i in range(n_bot_entrants):
        state = "CLAIMED" if i < n_bot_seats else "WAITLISTED"
        draw = "WON" if i < n_bot_seats else "WAITLISTED"
        rows.append({"user_id": f"b{i}", "label": "bot", "nat_group": -1, "srv_state": state,
                     "srv_draw_state": draw, "srv_arrival_seq": seq, "srv_weight": 1.0})
        seq += 1
    for i in range(n_human_absent):  # intended but never entered (lockout)
        rows.append({"user_id": f"hx{i}", "label": "legit", "nat_group": -1})
    return make_users(rows)


def test_bot_seat_share_exactly_20_percent():
    # 100 seats: 20 to bots, 80 to humans. 200 bot entrants, 800 human entrants.
    df = population(n_human_seats=80, n_bot_seats=20, n_human_entrants=800, n_bot_entrants=200)
    m = F.fairness_per_run(df)
    assert m["bot_seat_share"] == pytest.approx(0.20)
    assert m["bot_entrant_share"] == pytest.approx(0.20)  # lottery-neutral: seats == entrants share
    assert m["human_win_prob"] == pytest.approx(80 / 800)
    assert m["human_entry_success_rate"] == pytest.approx(1.0)
    assert m["all_final_seats"] == 100 and m["bot_final_seats"] == 20


def test_entry_success_drops_with_lockout():
    # 800 humans intended, 200 locked out (never entered) => success 600/800.
    df = population(n_human_seats=60, n_bot_seats=0, n_human_entrants=600, n_bot_entrants=0, n_human_absent=200)
    m = F.fairness_per_run(df, humans_intended=800)
    assert m["human_entry_success_rate"] == pytest.approx(600 / 800)
    assert m["human_win_prob"] == pytest.approx(60 / 800)


def test_fcfs_arrival_correlation_is_strongly_negative():
    # First 20 arrivals win, rest lose: earliest win => negative Spearman, tiny perm p.
    rows = [{"user_id": f"u{i}", "label": "legit", "nat_group": -1, "srv_state": "CLAIMED" if i < 20 else "WAITLISTED",
             "srv_draw_state": "WON" if i < 20 else "WAITLISTED", "srv_arrival_seq": i + 1, "srv_weight": 1.0}
            for i in range(200)]
    m = F.fairness_per_run(make_users(rows))
    # A binary outcome (20 winners of 200) caps |Spearman|; ~-0.52 here, still strongly
    # negative and far from the lottery's ~0. point-biserial agrees.
    assert m["arrival_time_correlation"] < -0.4
    assert m["arrival_point_biserial"] < -0.4
    p = F.pooled_arrival_perm_p([m["_corr_order"]], [m["_corr_won"]], n_perm=2000, seed=1)
    assert p < 0.01


def test_lottery_arrival_correlation_near_zero():
    # Winners every 10th arrival: no monotonic relation => correlation ~0, large perm p.
    rows = [{"user_id": f"u{i}", "label": "legit", "nat_group": -1,
             "srv_state": "CLAIMED" if i % 10 == 0 else "WAITLISTED",
             "srv_draw_state": "WON" if i % 10 == 0 else "WAITLISTED", "srv_arrival_seq": i + 1, "srv_weight": 1.0}
            for i in range(200)]
    m = F.fairness_per_run(make_users(rows))
    assert abs(m["arrival_time_correlation"]) < 0.05
    p = F.pooled_arrival_perm_p([m["_corr_order"]], [m["_corr_won"]], n_perm=2000, seed=1)
    assert p > 0.2


def test_win_counts_and_jain_gini_over_runs():
    # Fixed 4-person population; person h0 wins all 3 runs, others never win.
    frames = []
    for _ in range(3):
        rows = [{"user_id": "h0", "label": "legit", "srv_state": "CLAIMED"}]
        rows += [{"user_id": f"h{i}", "label": "legit", "srv_state": "WAITLISTED"} for i in (1, 2, 3)]
        frames.append(make_users(rows))
    counts = F.win_counts_by_identity(frames)
    assert sorted(counts.tolist()) == [0.0, 0.0, 0.0, 3.0]
    assert S.jain_index(counts) == pytest.approx(0.25)  # (3)^2 / (4 * 9) = 0.25 = 1/N
    assert S.gini(counts) == pytest.approx(0.75)  # one winner of four


def test_detection_confusion_on_known_labels():
    # 8 bots down-weighted (TP), 2 bots missed (FN), 2 humans wrongly flagged (FP), 88 clean (TN).
    rows = []
    for i in range(8):
        rows.append({"user_id": f"b{i}", "label": "bot", "nat_group": -1, "srv_state": "WAITLISTED", "srv_weight": 0.25})
    for i in range(2):
        rows.append({"user_id": f"bm{i}", "label": "bot", "nat_group": -1, "srv_state": "WAITLISTED", "srv_weight": 1.0})
    for i in range(2):
        rows.append({"user_id": f"hf{i}", "label": "legit", "nat_group": 1, "srv_state": "WAITLISTED", "srv_weight": 0.5})
    for i in range(88):
        rows.append({"user_id": f"h{i}", "label": "legit", "nat_group": -1, "srv_state": "WAITLISTED", "srv_weight": 1.0})
    m = D.detection_per_run(make_users(rows))
    assert m["precision"] == pytest.approx(8 / 10)
    assert m["recall"] == pytest.approx(8 / 10)
    assert m["false_positive_rate"] == pytest.approx(2 / 90)
    assert m["false_positive_rate_nat"] == pytest.approx(1.0)  # the 2 flagged humans: one is NAT, and it is the only NAT


def test_detection_none_when_no_defence():
    df = population(n_human_seats=10, n_bot_seats=2, n_human_entrants=100, n_bot_entrants=20)  # all weight 1.0
    m = D.detection_per_run(df)
    assert m["precision"] is None and m["recall"] is None


def test_cost_per_seat_ratio_of_sums():
    # Two runs: (1000 req, 5 seats) and (3000 req, 5 seats) => 4000/10 = 400 req/seat.
    st = S.ratio_stat([1000, 3000], [5, 5], resamples=2000, seed=0)
    assert st.mean == pytest.approx(400.0) and st.ci_low <= 400.0 <= st.ci_high and st.n == 2
    assert S.ratio_stat([10, 20], [0, 0]) is None  # no seats => unbounded


def test_forced_stat_contract():
    assert S.forced_stat([]) is None
    one = S.forced_stat([0.3])
    assert one.n == 1 and one.ci_low == one.ci_high == 0.3
    many = S.forced_stat([0.2, 0.25, 0.3, 0.22])
    assert many.ci_low <= many.mean <= many.ci_high and many.n == 4


def test_bootstrap_stat_over_identities():
    # 4 identities, one wins 3 times: Jain point = 0.25, with a CI and n = 4 identities.
    counts = np.array([3.0, 0.0, 0.0, 0.0])
    st = S.bootstrap_stat(counts, S.jain_index, resamples=2000, seed=1)
    assert st.n == 4 and st.mean == pytest.approx(0.25)
    assert st.ci_low <= st.mean <= st.ci_high
    assert S.bootstrap_stat(np.zeros(5), S.jain_index) is None  # all-zero undefined
