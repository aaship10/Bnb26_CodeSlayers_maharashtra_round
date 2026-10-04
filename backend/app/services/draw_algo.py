"""The draw, as pure functions (no database, no clock, no randomness of its own).

Encoding follows docs/DRAW_SPEC_PROVISIONAL.md, which the in-browser verifier
implements independently; tests/unit/test_draw_algo.py replays the verifier's
test vectors so the two cannot drift apart silently.

  entrants_hash = SHA-256( "public_id,weight" lines sorted by public_id, joined by "\\n" )
  final_seed    = SHA-256( server_seed || beacon_randomness || utf8(event_id) || entrants_hash )
  h(entrant)    = HMAC-SHA256( key = final_seed, msg = utf8(public_id) )
  order         = ascending h (all weights 1) or ascending -ln(u)/weight (any other weight)
  ties          = ascending public_id
"""
from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

WEIGHT_TEXT: dict[Decimal, str] = {Decimal(1): "1", Decimal("0.5"): "0.5", Decimal("0.25"): "0.25"}


@dataclass(frozen=True)
class Entrant:
    public_id: str      # printable ASCII, no comma/whitespace (uuid text in practice)
    weight: Decimal     # 1, 0.5 or 0.25; weight 0 never reaches the draw


@dataclass(frozen=True)
class DrawResult:
    entrants_hash: str  # hex
    final_seed: str     # hex
    order: list[str]    # every entrant's public_id in draw order; first `inventory` win
    winners: list[str]
    waitlist: list[str]
    winners_hash: str
    waitlist_hash: str


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def weight_text(w: Decimal | float | int) -> str:
    try:
        return WEIGHT_TEXT[Decimal(str(w))]
    except KeyError:
        raise ValueError(f"weight {w!r} is not one of 1, 0.5, 0.25") from None


def entrants_text(entrants: Iterable[Entrant]) -> str:
    rows = sorted(entrants, key=lambda e: e.public_id)
    return "\n".join(f"{e.public_id},{weight_text(e.weight)}" for e in rows)


def entrants_hash(entrants: Iterable[Entrant]) -> bytes:
    return sha256(entrants_text(entrants).encode("utf-8"))


def final_seed(server_seed: bytes, beacon: bytes, event_id: str, entrants_hash_: bytes) -> bytes:
    return sha256(server_seed + beacon + event_id.encode("utf-8") + entrants_hash_)


def list_hash(ids: Sequence[str]) -> str:
    return sha256("\n".join(ids).encode("utf-8")).hex()


def _ln_big(x: int) -> float:
    """ln of a big integer using only a 53-bit mantissa (same as the verifier)."""
    bits = x.bit_length()
    if bits <= 53:
        return math.log(x)
    s = bits - 53
    return math.log(x >> s) + s * math.log(2)


_LN_D = _ln_big(2**256 + 1)


def rank(entrants: Sequence[Entrant], seed: bytes) -> list[str]:
    """Draw order of all entrants under `seed`."""
    weighted = any(e.weight != 1 for e in entrants)
    rows: list[tuple[float | int, str]] = []
    for e in entrants:
        n = int.from_bytes(hmac.new(seed, e.public_id.encode("utf-8"), hashlib.sha256).digest(), "big")
        key: float | int = (_LN_D - _ln_big(n + 1)) / float(e.weight) if weighted else n
        rows.append((key, e.public_id))
    rows.sort()
    return [pid for _, pid in rows]


def run_draw(event_id: str, server_seed: bytes, beacon: bytes,
             entrants: Sequence[Entrant], inventory: int) -> DrawResult:
    e_hash = entrants_hash(entrants)
    seed = final_seed(server_seed, beacon, event_id, e_hash)
    order = rank(entrants, seed)
    winners, waitlist = order[:inventory], order[inventory:]
    return DrawResult(entrants_hash=e_hash.hex(), final_seed=seed.hex(), order=order,
                      winners=winners, waitlist=waitlist,
                      winners_hash=list_hash(winners), waitlist_hash=list_hash(waitlist))
