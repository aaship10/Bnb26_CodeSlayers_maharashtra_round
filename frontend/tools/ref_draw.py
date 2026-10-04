#!/usr/bin/env python3
"""
Independent reference for the Fair Drop draw and audit chain (PROVISIONAL).

A owns docs/DRAW_SPEC.md and the audit encoding. Until those exist, this script
pins down one precise reading of the shared conventions so the browser verifier,
the mock server and this script can be checked against each other. Every
encoding choice below is listed in docs/DRAW_SPEC_PROVISIONAL.md. When A's spec
lands, A's vectors are authoritative and any mismatch is a bug report to A.

Writes:
  src/features/fairness/vectors.json     small cases for unit tests (draw + audit)
  mock-server/fixtures/demo_draw.json    summary of the 50,000-entrant demo draw

    python tools/ref_draw.py
"""
import hashlib
import hmac
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ----------------------------------------------------------------- the draw

WEIGHT_TEXT = {1: "1", 0.5: "0.5", 0.25: "0.25"}


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def weight_text(w) -> str:
    if w not in WEIGHT_TEXT:
        raise ValueError(f"weight {w!r} not allowed")
    return WEIGHT_TEXT[w]


def entrants_text(entrants) -> str:
    rows = sorted(entrants, key=lambda e: e["public_id"])
    return "\n".join(f"{e['public_id']},{weight_text(e['weight'])}" for e in rows)


def final_seed(server_seed: bytes, beacon: bytes, event_id: str, entrants_hash: bytes) -> bytes:
    return sha256(server_seed + beacon + event_id.encode("utf-8") + entrants_hash)


def ln_big(x: int) -> float:
    b = x.bit_length()
    if b <= 53:
        return math.log(x)
    s = b - 53
    return math.log(x >> s) + s * math.log(2)


LN_D = ln_big(2**256 + 1)


def rank(entrants, seed: bytes):
    weighted = any(e["weight"] != 1 for e in entrants)
    rows = []
    for e in entrants:
        h = hmac.new(seed, e["public_id"].encode("utf-8"), hashlib.sha256).digest()
        n = int.from_bytes(h, "big")
        if weighted:
            key = (LN_D - ln_big(n + 1)) / e["weight"]
        else:
            key = n
        rows.append((key, e["public_id"], h.hex()))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows, weighted


def list_hash(ids) -> str:
    return sha256("\n".join(ids).encode("utf-8")).hex()


def run_draw(event_id: str, server_seed: bytes, beacon: bytes, entrants, inventory: int):
    text = entrants_text(entrants)
    e_hash = sha256(text.encode("utf-8"))
    seed = final_seed(server_seed, beacon, event_id, e_hash)
    rows, weighted = rank(entrants, seed)
    order = [r[1] for r in rows]
    winners, waitlist = order[:inventory], order[inventory:]
    return {
        "commitment": sha256(server_seed).hex(),
        "entrants_hash": e_hash.hex(),
        "final_seed": seed.hex(),
        "weighted": weighted,
        "order": order,
        "hmac": {r[1]: r[2] for r in rows},
        "winners": winners,
        "waitlist": waitlist,
        "winners_hash": list_hash(winners),
        "waitlist_hash": list_hash(waitlist),
    }


# --------------------------------------------------------- deterministic data

def pid(tag: str, i: int) -> str:
    return "p_" + hashlib.sha256(f"{tag}:{i}".encode()).hexdigest()[:12]


def mock_server_seed(event_id: str) -> bytes:
    return sha256(f"fd-mock-server-seed:{event_id}".encode())


def mock_beacon(event_id: str):
    rnd = 4_200_000 + int(hashlib.sha256(f"fd-mock-beacon-round:{event_id}".encode()).hexdigest()[:8], 16) % 100_000
    return rnd, sha256(f"fd-mock-beacon:{rnd}".encode())


def demo_entrants(n=50_000):
    return [{"public_id": pid("fd-demo-entrant", i), "weight": 1} for i in range(n)]


# ------------------------------------------------------------- audit chain

GENESIS = "0" * 64


def canonical_json(v) -> str:
    if isinstance(v, float):
        raise ValueError("floats are not allowed in audit payloads")
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical_json(v[k]) for k in sorted(v)) + "}"
    if isinstance(v, list):
        return "[" + ",".join(canonical_json(x) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False)


def audit_hash(prev_hash: str, rec) -> str:
    body = canonical_json({"seq": rec["seq"], "type": rec["type"], "ts": rec["ts"], "payload": rec["payload"]})
    return sha256((prev_hash + "\n" + body).encode("utf-8")).hex()


def chain(records):
    prev = GENESIS
    out = []
    for r in records:
        h = audit_hash(prev, r)
        out.append({**r, "prev_hash": prev, "hash": h, "canonical": canonical_json({"seq": r["seq"], "type": r["type"], "ts": r["ts"], "payload": r["payload"]})})
        prev = h
    return out


# --------------------------------------------------------------------- main

def case(name, event_id, entrants, inventory, full_order=True):
    server_seed = sha256(f"vector-seed:{name}".encode())
    beacon = sha256(f"vector-beacon:{name}".encode())
    r = run_draw(event_id, server_seed, beacon, entrants, inventory)
    out = {
        "name": name,
        "event_id": event_id,
        "server_seed": server_seed.hex(),
        "beacon_randomness": beacon.hex(),
        "inventory": inventory,
        "entrants": entrants,
        "expected": {
            "commitment": r["commitment"],
            "entrants_hash": r["entrants_hash"],
            "final_seed": r["final_seed"],
            "weighted": r["weighted"],
            "order": r["order"] if full_order else r["order"][:50],
            "winners_hash": r["winners_hash"],
            "waitlist_hash": r["waitlist_hash"],
            "winners_count": len(r["winners"]),
            "waitlist_count": len(r["waitlist"]),
        },
    }
    if full_order:
        out["expected"]["hmac"] = r["hmac"]
    return out


def main():
    cycle = [1, 0.5, 0.25]
    w2000 = {0: 1, 1: 0.5, 2: 0.25, 3: 1}
    draw_cases = [
        case("ten-unweighted", "evt_vec_a", [{"public_id": pid("vec-a", i), "weight": 1} for i in range(10)], 3),
        case("mixed-weights", "evt_vec_b", [{"public_id": pid("vec-b", i), "weight": cycle[i % 3]} for i in range(12)], 4),
        case("zero-entrants", "evt_vec_c", [], 5),
        case("fewer-than-seats", "evt_vec_d", [{"public_id": pid("vec-d", i), "weight": 1} for i in range(5)], 10),
        case("weighted-2000", "evt_vec_e", [{"public_id": pid("vec-e", i), "weight": w2000[i % 4]} for i in range(2000)], 200, full_order=False),
    ]

    audit_records = [
        {"seq": 1, "type": "EVENT_CREATED", "ts": "2026-11-01T09:00:00.000Z", "payload": {"name": "Vector Night", "inventory": 3, "mode": "LOTTERY"}},
        {"seq": 2, "type": "SEED_COMMITTED", "ts": "2026-11-01T09:00:01.000Z", "payload": {"commitment": "ab" * 32}},
        {"seq": 3, "type": "CONFIG_SET", "ts": "2026-11-01T09:01:00.000Z", "payload": {"defences": {"preset": "custom", "layers": {"pow": {"enabled": True, "difficulty_bits": 18}, "captcha": {"enabled": False}}}, "note": None}},
        {"seq": 4, "type": "WINDOW_CLOSED", "ts": "2026-11-01T10:30:00.000Z", "payload": {"entrants_count": 10, "zz": [3, 1, 2], "aa": "first"}},
        {"seq": 5, "type": "NOTE", "ts": "2026-11-01T10:31:00.000Z", "payload": {"text": "Zażółć \"gęślą\" jaźń / tab\tnewline\n emoji 🎟️ \u0001"}},
    ]

    vectors = {
        "spec": "PROVISIONAL fd-draw/1 (see docs/DRAW_SPEC_PROVISIONAL.md). Replace with A's vectors from docs/DRAW_SPEC.md.",
        "draw": draw_cases,
        "audit": {"genesis": GENESIS, "records": chain(audit_records)},
    }
    with open(os.path.join(ROOT, "src", "features", "fairness", "vectors.json"), "w", encoding="utf-8") as f:
        json.dump(vectors, f, ensure_ascii=False, indent=1)

    # 50,000-entrant demo draw for evt_demo_01 (unweighted), as served by the mock.
    eid = "evt_demo_01"
    rnd, beacon = mock_beacon(eid)
    entrants = demo_entrants()
    r = run_draw(eid, mock_server_seed(eid), beacon, entrants, 500)
    demo = {
        "event_id": eid,
        "entrant_rule": 'public_id = "p_" + sha256("fd-demo-entrant:" + i)[:12 hex], weight 1, i = 0..49999',
        "server_seed": mock_server_seed(eid).hex(),
        "commitment": r["commitment"],
        "beacon": {"source": "mock-beacon", "round": rnd, "randomness": beacon.hex()},
        "entrants_count": len(entrants),
        "entrants_hash": r["entrants_hash"],
        "final_seed": r["final_seed"],
        "inventory": 500,
        "winners_hash": r["winners_hash"],
        "waitlist_hash": r["waitlist_hash"],
        "first_winners": r["winners"][:10],
        "first_waitlisted": r["waitlist"][:10],
    }
    os.makedirs(os.path.join(ROOT, "mock-server", "fixtures"), exist_ok=True)
    with open(os.path.join(ROOT, "mock-server", "fixtures", "demo_draw.json"), "w", encoding="utf-8") as f:
        json.dump(demo, f, ensure_ascii=False, indent=2)
    print("vectors:", [c["name"] for c in draw_cases], "| audit records:", len(audit_records))
    print("demo:", demo["entrants_hash"][:16], demo["winners_hash"][:16], demo["first_winners"][:3])


if __name__ == "__main__":
    main()
