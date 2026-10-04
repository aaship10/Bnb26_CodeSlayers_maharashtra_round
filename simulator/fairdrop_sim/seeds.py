"""Seed derivation. One master seed -> independent, named sub-seeds.

derive_seed(master, "run", 3, "arrivals") is stable across Python versions and
processes (no hash randomisation), so any run can be reproduced from its record.
"""
from __future__ import annotations

import hashlib


def derive_seed(master: int, *labels: object) -> int:
    """63-bit seed from a master seed and a label path."""
    h = hashlib.sha256(str(master).encode())
    for label in labels:
        h.update(b"/")
        h.update(str(label).encode())
    return int.from_bytes(h.digest()[:8], "big") >> 1


def derive_seed_hex(master: int, *labels: object) -> str:
    """32-byte hex seed (for the server draw seed, server_seed_hex)."""
    h = hashlib.sha256(f"server-seed/{master}".encode())
    for label in labels:
        h.update(b"/")
        h.update(str(label).encode())
    return h.hexdigest()
