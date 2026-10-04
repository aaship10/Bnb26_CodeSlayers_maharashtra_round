"""Detection quality of the defences vs ground truth (METRICS.md §4). Unit = identity.

Scope note (surfaced in FINDINGS.md): this scores the *risk/signals* layers, whose
signal is a down-weighted or excluded entry (srv_weight < 1.0) or an explicit reject
decision. PoW/CAPTCHA do not "detect" an identity, they price it; a bot they lock out
simply never enters, which the entry-success metric captures, not this one. So detection
is computed over ENTRANTS (who have a weight) plus any rejected identities from the
decisions log.
"""
from __future__ import annotations

import pandas as pd

from fairdrop_sim.metrics import stats as S

BOT, HUMAN = "bot", "legit"


def flagged_mask(df: pd.DataFrame, rejected_ids: set[str] | None = None) -> pd.Series:
    """True where a defence acted against the identity: weight < 1.0 (down-weighted or
    excluded) or an explicit reject decision."""
    weight = pd.to_numeric(df["srv_weight"], errors="coerce")
    mask = weight.notna() & (weight < 1.0)
    if rejected_ids:
        mask = mask | df["user_id"].isin(rejected_ids)
    return mask


def detection_per_run(df: pd.DataFrame, rejected_ids: set[str] | None = None) -> dict:
    """precision / recall / FPR over entrants (+ rejected), and FPR restricted to NAT
    users. None when no defence produced a positive (nothing to score)."""
    scored = df[df["srv_state"].notna() | df["user_id"].isin(rejected_ids or set())].copy()
    if scored.empty:
        return _none()
    flagged = flagged_mask(scored, rejected_ids)
    is_bot = scored["label"] == BOT
    is_human = ~is_bot
    tp = int((flagged & is_bot).sum())
    fp = int((flagged & is_human).sum())
    fn = int((~flagged & is_bot).sum())
    tn = int((~flagged & is_human).sum())
    if tp + fp == 0:
        return _none(have_humans=is_human.sum())
    rates = S.confusion_rates(tp, fp, fn, tn)
    nat = scored[is_human & (pd.to_numeric(scored.get("nat_group", -1), errors="coerce") >= 0)]
    nat_fp = int(flagged_mask(nat, rejected_ids).sum()) if len(nat) else 0
    rates["false_positive_rate_nat"] = nat_fp / len(nat) if len(nat) else None
    rates["_counts"] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "nat": len(nat), "nat_fp": nat_fp}
    return rates


def _none(have_humans: int = 0) -> dict:
    return {"precision": None, "recall": None, "false_positive_rate": None,
            "false_positive_rate_nat": None, "_counts": {"tp": 0, "fp": 0, "fn": 0, "tn": int(have_humans)}}


def rejected_from_decisions(decisions: list[dict]) -> set[str]:
    return {d["user_id"] for d in decisions if d.get("action") == "reject" and d.get("user_id")}
