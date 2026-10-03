#!/usr/bin/env python3
"""
Independent reference for the proof-of-work rule in the shared conventions:

    find a decimal-string nonce such that SHA-256(utf8(prefix + ":" + nonce))
    has at least difficulty_bits leading zero bits.

Prints the smallest such nonce (searching 0, 1, 2, ...) for a set of prefixes.
These are the provisional vectors baked into
src/features/challenge/pow.vectors.ts. B's docs/POW_SPEC.md vectors are
authoritative once they land; a mismatch is a bug report to B, not a silent patch.

    python tools/ref_pow.py
"""
import hashlib
import json


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        bits += 8 - byte.bit_length()
        break
    return bits


def solve(prefix: str, bits: int) -> int:
    n = 0
    while True:
        d = hashlib.sha256(f"{prefix}:{n}".encode("utf-8")).digest()
        if leading_zero_bits(d) >= bits:
            return n
        n += 1


# Short prefix, a typical id-bearing prefix, and prefixes whose lengths straddle
# the 55/56-byte single-block boundary and the 64-byte block size.
PREFIXES = [
    "vector-a",
    "fd1.evt_demo_01.3fa91c20.7",
    "x" * 50,  # prefix + ':' = 51 bytes, nonce digits push it across 55
    "y" * 54,  # 55 bytes before the nonce: any digit forces a second block
    "z" * 63,  # 64 bytes: prefix + ':' fills block 0 exactly
    "fd1.7c1f5b0e-3d52-4b7a-9c61-0a4d8e2f6b13.0f6c1a22-77de-5b1e-a301-9d4e5a6b7c88.42",  # long, realistic
]
BITS = [0, 1, 8, 12, 16]

out = []
for p in PREFIXES:
    for b in BITS:
        out.append({"prefix": p, "bits": b, "nonce": str(solve(p, b))})
# one heavier case, the mock server default
out.append({"prefix": "vector-a", "bits": 18, "nonce": str(solve("vector-a", 18))})

print(json.dumps(out, indent=2))
