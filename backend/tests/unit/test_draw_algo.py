"""The draw encoding against the verifier's independent test vectors.

frontend/src/features/fairness/vectors.json is produced by frontend/tools/ref_draw.py
(a separate implementation). If this fails, the server and the in-browser verifier
disagree on the encoding: fix one of them, never the vectors.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import draw_algo
from app.services.draw_algo import Entrant

VECTORS = Path(__file__).resolve().parents[3] / "frontend/src/features/fairness/vectors.json"
CASES = json.loads(VECTORS.read_text(encoding="utf-8"))["draw"] if VECTORS.exists() else []


def _entrants(case):
    return [Entrant(e["public_id"], Decimal(str(e["weight"]))) for e in case["entrants"]]


@pytest.mark.skipif(not CASES, reason="frontend vectors not present")
@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_matches_verifier_vectors(case):
    r = draw_algo.run_draw(case["event_id"], bytes.fromhex(case["server_seed"]),
                           bytes.fromhex(case["beacon_randomness"]), _entrants(case), case["inventory"])
    exp = case["expected"]
    assert draw_algo.sha256(bytes.fromhex(case["server_seed"])).hex() == exp["commitment"]
    assert r.entrants_hash == exp["entrants_hash"]
    assert r.final_seed == exp["final_seed"]
    assert r.order[:len(exp["order"])] == exp["order"]
    assert r.winners_hash == exp["winners_hash"]
    assert r.waitlist_hash == exp["waitlist_hash"]
    assert len(r.winners) == exp["winners_count"] and len(r.waitlist) == exp["waitlist_count"]


def test_input_order_does_not_matter():
    es = [Entrant(f"id{i:03}", Decimal(1)) for i in range(50)]
    a = draw_algo.run_draw("e", b"s" * 32, b"b" * 32, es, 10)
    b = draw_algo.run_draw("e", b"s" * 32, b"b" * 32, list(reversed(es)), 10)
    assert a == b


def test_every_input_changes_the_outcome():
    es = [Entrant(f"id{i:03}", Decimal(1)) for i in range(50)]
    base = draw_algo.run_draw("e", b"s" * 32, b"b" * 32, es, 10)
    assert draw_algo.run_draw("e", b"S" * 32, b"b" * 32, es, 10).final_seed != base.final_seed
    assert draw_algo.run_draw("e", b"s" * 32, b"B" * 32, es, 10).final_seed != base.final_seed
    assert draw_algo.run_draw("f", b"s" * 32, b"b" * 32, es, 10).final_seed != base.final_seed
    assert draw_algo.run_draw("e", b"s" * 32, b"b" * 32, es[:-1], 10).final_seed != base.final_seed


def test_unsupported_weight_is_rejected():
    with pytest.raises(ValueError):
        draw_algo.weight_text(Decimal("0.3"))
    assert draw_algo.weight_text(Decimal("1.0000")) == "1"
    assert draw_algo.weight_text(Decimal("0.5000")) == "0.5"


def test_lower_weight_wins_less_often():
    # 2000 entrants, weights 1 vs 0.25, 400 seats: heavier group must win clearly more.
    es = [Entrant(f"h{i:04}", Decimal(1)) for i in range(1000)] + \
         [Entrant(f"l{i:04}", Decimal("0.25")) for i in range(1000)]
    r = draw_algo.run_draw("e", b"s" * 32, b"b" * 32, es, 400)
    heavy = sum(p.startswith("h") for p in r.winners)
    assert heavy > 300
