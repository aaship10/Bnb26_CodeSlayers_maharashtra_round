"""Reference proof-of-work solver in Python (for Member C's simulator). Standalone: stdlib only.

    python scripts/pow_solver.py <challenge-id-or-prefix> <difficulty_bits>

Rule (docs/POW_SPEC.md): find the smallest decimal nonce n >= 0 such that
SHA-256(utf8(prefix + ":" + str(n))) has at least `bits` leading zero bits.
Send it as  X-Challenge-Id: <challenge id>   X-Challenge-Solution: <n>.

Import use:   from pow_solver import solve;  nonce = solve(challenge["pow"]["prefix"], challenge["pow"]["difficulty_bits"])
"""
from __future__ import annotations

import hashlib
import sys
import time


def solve(prefix: str, bits: int, start: int = 0, limit: int = 1 << 40) -> str:
    base = hashlib.sha256()
    base.update(f"{prefix}:".encode())  # hash the prefix once; copy() per attempt
    n = start
    while n < limit:
        h = base.copy()
        h.update(str(n).encode())
        # leading zero bits of a 256-bit big-endian number
        if 256 - int.from_bytes(h.digest(), "big").bit_length() >= bits:
            return str(n)
        n += 1
    raise RuntimeError("no solution within limit")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    t = time.perf_counter()
    nonce = solve(sys.argv[1], int(sys.argv[2]))
    print(f"nonce={nonce}  ({int(nonce) + 1} attempts, {time.perf_counter() - t:.3f}s)")
