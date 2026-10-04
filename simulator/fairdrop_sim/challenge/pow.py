"""Proof-of-work client solver (shared conventions):

    find a decimal-string nonce such that SHA-256(utf8(prefix + ":" + nonce))
    has at least difficulty_bits leading zero bits.

Tested against D's vectors (frontend/tools/ref_pow.py). B's docs/POW_SPEC.md
vectors are authoritative once they land; a mismatch is reported to B.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        return bits + 8 - byte.bit_length()
    return bits


def check(prefix: str, nonce: str, difficulty_bits: int) -> bool:
    d = hashlib.sha256(f"{prefix}:{nonce}".encode("utf-8")).digest()
    return leading_zero_bits(d) >= difficulty_bits


@dataclass(frozen=True)
class PowSolution:
    nonce: str
    hashes: int  # attempts made, i.e. the real work done (cost accounting)


def solve(prefix: str, difficulty_bits: int, start: int = 0, max_hashes: int | None = None) -> PowSolution | None:
    """Smallest nonce >= start. Returns None if max_hashes is exhausted.

    Uses a pre-hashed prefix (hashlib copy) so each attempt only hashes the nonce."""
    base = hashlib.sha256(f"{prefix}:".encode("utf-8"))
    full_bytes, rem = divmod(difficulty_bits, 8)
    mask = (0xFF << (8 - rem)) & 0xFF if rem else 0
    n = start
    tried = 0
    while max_hashes is None or tried < max_hashes:
        h = base.copy()
        h.update(str(n).encode())
        d = h.digest()
        tried += 1
        if not any(d[:full_bytes]) and (not mask or not d[full_bytes] & mask):
            return PowSolution(str(n), tried)
        n += 1
    return None


def expected_hashes(difficulty_bits: int) -> float:
    return float(2**difficulty_bits)
