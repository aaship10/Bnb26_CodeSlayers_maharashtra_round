"""Fairness metrics from a per-client frame (METRICS.md §2). Pure functions over a
pandas DataFrame so fixtures with known answers can test them directly.

Expected columns (as written by engine/run.py `users.csv.gz`):
  label        'legit' | 'bot'            (ground truth, simulator-side only)
  srv_state    final server state, NaN if the client never entered
  srv_draw_state  state right after the draw, NaN if not entered
  srv_arrival_seq server arrival order, NaN if not entered
  srv_weight   draw weight (1.0 unless a defence down-weighted it)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from fairdrop_sim.metrics import stats as S

WIN_STATES = {"WON", "CLAIMED"}
BOT, HUMAN = "bot", "legit"


def _entrants(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["srv_state"].notna()]


def fairness_per_run(df: pd.DataFrame, humans_intended: int | None = None) -> dict:
    """Scalar fairness values for ONE run. `humans_intended` defaults to every legit row
    (all simulated humans intend to enter). Shares are None when their denominator is 0."""
    ent = _entrants(df)
    is_bot = ent["label"] == BOT
    seats = ent["srv_state"] == "CLAIMED"
    all_seats = int(seats.sum())
    bot_seats = int((seats & is_bot).sum())
    all_ent = int(len(ent))
    bot_ent = int(is_bot.sum())
    humans_intended = humans_intended if humans_intended is not None else int((df["label"] == HUMAN).sum())
    human_entrants = int((ent["label"] == HUMAN).sum())
    human_seats = all_seats - bot_seats

    won = ent["srv_draw_state"].isin(WIN_STATES).to_numpy().astype(int)
    order = ent["srv_arrival_seq"].to_numpy(dtype=float)
    corr = S.spearman(order, won)
    pb = S.point_biserial(won, order)
    # Normalise arrival order to [0,1] within this run so runs can be pooled for one
    # permutation test (raw arrival_seq is not comparable across runs of different size).
    norm = (pd.Series(order).rank().to_numpy() - 1) / max(len(order) - 1, 1) if len(order) else np.zeros(0)

    return {
        "bot_seat_share": bot_seats / all_seats if all_seats else None,
        "bot_entrant_share": bot_ent / all_ent if all_ent else None,
        "human_win_prob": human_seats / humans_intended if humans_intended else None,
        "human_entry_success_rate": human_entrants / humans_intended if humans_intended else None,
        # raw counts so the aggregator can pool a Wilson interval (brief: Wilson for proportions)
        "_human_seats": human_seats,
        "_human_entrants": human_entrants,
        "_humans_intended": humans_intended,
        "arrival_time_correlation": corr,
        "arrival_point_biserial": pb,
        "all_final_seats": all_seats,
        "bot_final_seats": bot_seats,
        "all_entrants": all_ent,
        "bot_entrants": bot_ent,
        "_corr_order": norm,  # pooled by the aggregator for the permutation test
        "_corr_won": won,
    }


def win_counts_by_identity(frames: list[pd.DataFrame], label: str | None = HUMAN,
                           win_col: str = "srv_state") -> np.ndarray:
    """Per-identity win count across runs on the FIXED population (for Jain/Gini).
    A win = final seat (srv_state == CLAIMED). Identities that never win still count
    (they contribute 0), so the spread over the whole population is measured."""
    counts: dict[str, int] = {}
    pop: set[str] = set()
    for df in frames:
        sub = df if label is None else df[df["label"] == label]
        pop.update(sub["user_id"])
        winners = sub[sub[win_col] == "CLAIMED"]["user_id"]
        for uid in winners:
            counts[uid] = counts.get(uid, 0) + 1
    return np.array([counts.get(uid, 0) for uid in sorted(pop)], dtype=float)


def pooled_arrival_perm_p(order_parts: list[np.ndarray], won_parts: list[np.ndarray],
                          n_perm: int = 10_000, seed: int = 0) -> float | None:
    order = np.concatenate(order_parts) if order_parts else np.zeros(0)
    won = np.concatenate(won_parts) if won_parts else np.zeros(0)
    if order.size < 2:
        return None
    return S.permutation_pvalue(order, won, n_perm=n_perm, seed=seed)
