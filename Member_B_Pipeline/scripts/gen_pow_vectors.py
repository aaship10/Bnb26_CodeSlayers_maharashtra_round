"""Generate docs/pow_vectors.json with code that shares NOTHING with app.defence.pow.

Everything here uses only hashlib/hmac and integer arithmetic, written from the rule in POW_SPEC.md, so
the vectors are an independent check on the implementation rather than a copy of it.

    python scripts/gen_pow_vectors.py            # writes docs/pow_vectors.json
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "pow_vectors.json"


def zero_bits(prefix: str, nonce: int) -> int:
    digest = hashlib.sha256(f"{prefix}:{nonce}".encode()).digest()
    as_int = int.from_bytes(digest, "big")
    return 256 - as_int.bit_length()  # leading zero bits of a 256-bit big-endian number


def smallest_nonce(prefix: str, bits: int) -> int:
    n = 0
    while zero_bits(prefix, n) < bits:
        n += 1
    return n


def token(key: str, kind: str, pow_ok: int, event: str, user: str, exp: int, bits: int, nonce: str) -> str:
    msg = f"fd1|{kind}{pow_ok}|{event}|{user}|{exp}|{bits}|{nonce}"
    mac = hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:24]
    return f"fd1.{kind}{pow_ok}.{event}.{user}.{exp}.{bits}.{nonce}.{mac}"


def main() -> None:
    rule = []
    for prefix in ("vector-a", "fd1.example.prefix.42", "x" * 64, "y" * 129):
        for bits in (0, 1, 4, 8, 12, 14, 16):
            n = smallest_nonce(prefix, bits)
            rule.append({"prefix": prefix, "bits": bits, "nonce": str(n),
                         "sha256": hashlib.sha256(f"{prefix}:{n}".encode()).hexdigest()})

    key = "test-key-0123456789abcdef0123456789abcdef"
    event = "11111111111111111111111111111111"
    user = "22222222222222222222222222222222"
    tokens = []
    for kind, pow_ok, bits, exp, nonce in (("p", 0, 14, 2000000000, "0123456789abcdef"), ("p", 0, 8, 2000000000, "fedcba9876543210"),
                                           ("c", 1, 0, 2000000000, "00000000000000aa"), ("c", 0, 0, 2000000000, "00000000000000bb")):
        t = token(key, kind, pow_ok, event, user, exp, bits, nonce)
        entry = {"kind": kind, "pow_ok": pow_ok, "exp": exp, "bits": bits, "nonce": nonce, "token": t}
        if kind == "p":
            entry["solution"] = str(smallest_nonce(t, bits))
        tokens.append(entry)

    doc = {
        "rule": "SHA-256(utf8(prefix + ':' + nonce)) has >= bits leading zero bits; nonce is a decimal string; vectors give the SMALLEST nonce",
        "rule_vectors": rule,
        "token_vectors": {"key": key, "event_hex": event, "user_hex": user, "tokens": tokens},
    }
    OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(rule)} rule vectors, {len(tokens)} token vectors)")


if __name__ == "__main__":
    main()
