"""Offline measurement: what do the signals + risk engine do to the SEEDED population?

    python scripts/measure_risk.py [--users 50000] [--seed 1337] [--json out.json]

Runs the real feature functions and risk engine (app.defence.signals.features, app.defence.risk.engine) over
the deterministic population from scripts/seed_identities.py, in memory: no database, nothing touched.
Counting (accounts per device / IP / subnet / ASN, burst in a 10-minute bucket) is done in Python here and in
SQL in production; tests/test_risk_gate.py checks the SQL path on crafted data.

Ground truth: the seeder's cluster names are used ONLY in this analysis script to compute false-positive and
detection rates. No defence code ever sees them. Everything below is SYNTHETIC: my own generator's bots are as
clumsy or as clever as I made them, so read the numbers as "does the machinery behave sensibly", not as evidence
about real attackers. Two attacker models:
  A  "careful": bots send ordinary browser headers (the best case for the attacker)
  B  "scripted": bots use a script's default User-Agent (header_anomaly fires)
Timing regularity needs live request logs and is not modelled (always 0).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "infra" / "stub_api"))

from app.defence.config_schema import RiskThresholds, SignalsLayer  # noqa: E402
from app.defence.risk.engine import assess  # noqa: E402
from app.defence.signals import features as f  # noqa: E402
from app.defence.signals.asn import asn_of  # noqa: E402

spec = importlib.util.spec_from_file_location("seed_identities", ROOT / "scripts" / "seed_identities.py")
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)

CFG, TH = SignalsLayer(enabled=True), RiskThresholds()  # replaced by --weight overrides in main()
PLANTED = ("sequential", "one_device", "burst", "random_local")


def evaluate(rows: list[dict], scripted_bots: bool) -> list[dict]:
    dev = Counter(r["device"] for r in rows)
    ip = Counter(r["ip"] for r in rows)
    net = Counter(r["subnet"] for r in rows)
    asn_cidr = {r["ip"]: (asn_of(r["ip"]) or (None, None, None)) for r in rows}
    asn = Counter(a[1] for a in asn_cidr.values() if a[1])
    vel = Counter((r["subnet"], int(r["verified"].timestamp() // 600)) for r in rows)
    now = seed.ANCHOR  # evaluate at the moment of the drop

    out = []
    for r in rows:
        headers = {"user-agent": "python-requests/2.31"} if (scripted_bots and r["cluster"] in PLANTED) else \
                  {"user-agent": "Mozilla/5.0 Chrome/126", "accept-language": "en-IN"}
        a = asn_cidr[r["ip"]]
        signals = [
            f.header_anomaly(headers),
            f.timing_regularity([], CFG.timing_min_samples),
            f.accounts_per_device(dev[r["device"]]),
            f.accounts_per_ip(ip[r["ip"]]),
            f.accounts_per_subnet(net[r["subnet"]]),
            f.registration_velocity(max(0, vel[(r["subnet"], int(r["verified"].timestamp() // 600))] - 1)),
            f.account_age((now - r["verified"]).total_seconds()),
            f.email_pattern(r["flags"]),
            f.email_entropy(r["flags"]),
            f.otp_latency(r["latency"]),
        ]
        if a[1]:
            signals.append(f.accounts_per_asn(asn[a[1]], a[0]))
        risk = assess(signals, CFG, TH)
        out.append({"cluster": r["cluster"], "ip": r["ip"], "score": risk.score, "weight": risk.weight, "band": risk.band,
                    "challenge": risk.challenge_required,
                    "drivers": [p.name for p in risk.parts if p.contribution > 0 and p.name not in f.IP_ONLY_SIGNALS]})
    return out


def summarise(res: list[dict]) -> dict:
    by = defaultdict(list)
    for x in res:
        by[x["cluster"]].append(x)
        if x["cluster"] == "legit" and x["ip"] in seed.CAMPUS_NAT:
            by["legit (campus NAT)"].append(x)
    table = {}
    for name, xs in by.items():
        n = len(xs)
        table[name] = {
            "n": n,
            "mean_score": round(sum(x["score"] for x in xs) / n, 3),
            "challenged_pct": round(100 * sum(x["challenge"] for x in xs) / n, 2),
            "half_or_worse_pct": round(100 * sum(x["weight"] <= 0.5 for x in xs) / n, 2),
            "quarter_pct": round(100 * sum(x["weight"] == 0.25 for x in xs) / n, 2),
        }
    planted = [x for x in res if x["cluster"] in PLANTED]
    legit = [x for x in res if x["cluster"] == "legit"]
    tp = sum(x["challenge"] for x in planted)
    fp = sum(x["challenge"] for x in legit)
    table["_detection_at_challenge_threshold"] = {
        "recall_pct": round(100 * tp / len(planted), 2),
        "precision_pct": round(100 * tp / max(1, tp + fp), 2),
        "false_positive_rate_pct": round(100 * fp / len(legit), 3),
        "legit_students_challenged": fp,
    }
    return table


def explain_false_positives(res: list[dict]) -> None:
    fp = [x for x in res if x["cluster"] == "legit" and x["challenge"]]
    if not fp:
        return
    drivers = Counter(d for x in fp for d in x["drivers"])
    campus = sum(x["ip"] in seed.CAMPUS_NAT for x in fp)
    print(f"  why legitimate students were challenged ({len(fp)}): {campus} are behind the campus NAT (their network evidence sits at the cap);"
          f" non-network signals present: {dict(drivers.most_common())}")


def show(title: str, table: dict) -> None:
    print(f"\n== {title} ==")
    print(f"{'cluster':<22}{'n':>7}{'mean':>8}{'challenged%':>13}{'<=0.5 wt %':>12}{'0.25 wt %':>11}")
    for name in ("legit", "legit (campus NAT)", *PLANTED):
        t = table[name]
        print(f"{name:<22}{t['n']:>7}{t['mean_score']:>8}{t['challenged_pct']:>13}{t['half_or_worse_pct']:>12}{t['quarter_pct']:>11}")
    d = table["_detection_at_challenge_threshold"]
    print(f"detection at the challenge threshold ({TH.challenge}): recall {d['recall_pct']}%, precision {d['precision_pct']}%, "
          f"false-positive rate {d['false_positive_rate_pct']}% ({d['legit_students_challenged']} legitimate students)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=50_000)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--json")
    ap.add_argument("--weight", action="append", default=[], metavar="SIGNAL=VALUE",
                    help="override a signal weight to see its effect, e.g. --weight accounts_per_device=0.6")
    ap.add_argument("--ip-cap", type=float, help="override signals.ip_only_cap")
    a = ap.parse_args()
    global CFG
    overrides = {k: float(v) for k, v in (w.split("=", 1) for w in a.weight)}
    base = SignalsLayer(enabled=True)
    CFG = SignalsLayer(enabled=True, weights={**base.weights, **overrides}, ip_only_cap=a.ip_cap if a.ip_cap is not None else base.ip_only_cap)
    if overrides or a.ip_cap is not None:
        print("overrides:", overrides, {"ip_only_cap": CFG.ip_only_cap})

    g = seed.build(a.users, a.seed)
    seed.sequential_flags(g.rows)
    print(f"population: {len(g.rows)} identities (seed {a.seed}); thresholds challenge {TH.challenge} / half {TH.weight_half} / quarter {TH.weight_quarter}; "
          f"ip-only cap {CFG.ip_only_cap}")
    result = {}
    for key, title, scripted in (("A_careful", "A: careful bots (browser headers)", False), ("B_scripted", "B: scripted bots (script User-Agent)", True)):
        res = evaluate(g.rows, scripted)
        table = summarise(res)
        result[key] = table
        show(title, table)
        explain_false_positives(res)
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
