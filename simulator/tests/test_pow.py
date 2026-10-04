"""PoW solver vs D's vectors (frontend/src/features/challenge/pow.vectors.ts) and the mock verifier."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from fairdrop_sim.challenge.pow import check, leading_zero_bits, solve
from mock_server.defences import verify_pow

VECTORS_TS = Path(__file__).resolve().parents[2] / "frontend/src/features/challenge/pow.vectors.ts"
# Fallback copy so the suite still runs if the frontend tree is absent.
FALLBACK = [("vector-a", 8, "201"), ("vector-a", 12, "1965"), ("vector-a", 16, "41986"),
            ("fd1.evt_demo_01.3fa91c20.7", 12, "14517"), ("y" * 54, 16, "101806")]


def load_vectors() -> list[tuple[str, int, str]]:
    if not VECTORS_TS.exists():
        return FALLBACK
    pat = re.compile(r"\{ prefix: '([^']*)', bits: (\d+), nonce: '(\d+)' \}")
    vs = [(p, int(b), n) for p, b, n in pat.findall(VECTORS_TS.read_text(encoding="utf-8"))]
    assert len(vs) >= 30, "could not parse D's vectors"
    return vs


@pytest.mark.parametrize("prefix,bits,nonce", load_vectors())
def test_solver_matches_reference(prefix, bits, nonce):
    sol = solve(prefix, bits)
    assert sol is not None and sol.nonce == nonce
    assert sol.hashes == int(nonce) + 1
    assert check(prefix, nonce, bits) and verify_pow(prefix, nonce, bits)


def test_leading_zero_bits():
    assert leading_zero_bits(b"\x00\x00\x80") == 16
    assert leading_zero_bits(b"\x00\x01") == 15
    assert leading_zero_bits(b"\xff") == 0
    assert leading_zero_bits(b"\x00" * 32) == 256


def test_verifier_rejects_garbage():
    assert not verify_pow("p", "", 0)
    assert not verify_pow("p", "-1", 0)
    assert not verify_pow("p", "1e3", 0)
    assert not verify_pow("p", "١٢", 0)  # non-ASCII digits
    assert not verify_pow("p", "1" * 21, 0)


def test_max_hashes_budget():
    assert solve("vector-a", 16, max_hashes=1000) is None
