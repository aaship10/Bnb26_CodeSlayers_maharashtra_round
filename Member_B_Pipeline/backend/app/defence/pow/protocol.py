"""Stateless, HMAC-signed challenge tokens + the proof-of-work check.  (Spec: docs/POW_SPEC.md)

The challenge id IS the PoW prefix:

    fd1.<kind><pow_ok>.<event32>.<user32>.<exp>.<bits>.<nonce16>.<mac24>

    kind     'p' = proof-of-work challenge, 'c' = CAPTCHA challenge
    pow_ok   '1' = the PoW requirement is already satisfied (a CAPTCHA challenge issued after a
             solved PoW carries this signed bit, so the chain works with ONE solution header)
    event32 / user32   uuid hex: the challenge is bound to one user and one event
    exp      unix seconds; bits  required leading zero bits (0 for CAPTCHA)
    nonce16  random: two challenges for the same user/event are never identical
    mac24    first 96 bits of HMAC-SHA256(key, "fd1|<kind><pow_ok>|event|user|exp|bits|nonce")

The client finds a decimal nonce n such that SHA-256(utf8(id + ":" + n)) has >= bits leading zero
bits. Verification is one HMAC plus one SHA-256, with no database or Redis lookup. Because the
difficulty is inside the signed token, a client cannot ask for an easier one.
Prefix length: 3+1+2 + 1+32 + 1+32 + 1+10 + 1+(1..2) + 1+16 + 1+24 = 128..129 characters, i.e. 3 SHA-256
blocks of which only the last is recomputed per attempt by a midstate-caching solver.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from enum import Enum

VERSION = "fd1"
MAC_HEX = 24
MAX_SOLUTION_LEN = 20  # decimal digits; bounds the work a hostile header can cause
# fullmatch, not ^...$: in Python `$` also matches just before a trailing newline, so "12" + newline slips through.
_SOLUTION = re.compile(r"[0-9]{1,%d}" % MAX_SOLUTION_LEN)
_TOKEN = re.compile(
    r"fd1\.([pc])([01])\.([0-9a-f]{32})\.([0-9a-f]{32})\.([0-9]{1,12})\.([0-9]{1,2})\.([0-9a-f]{16})\.([0-9a-f]{%d})" % MAC_HEX
)


class Status(str, Enum):
    OK = "ok"
    FORGED = "forged"  # malformed or bad MAC: hard evidence of tampering
    WRONG_BINDING = "wrong_binding"  # genuine token, but issued to another user/event
    EXPIRED = "expired"


@dataclass(frozen=True)
class Token:
    raw: str
    kind: str  # "p" | "c"
    pow_ok: bool
    event: str  # uuid hex
    user: str
    exp: int
    bits: int
    nonce: str
    mac: str


def _mac(key: str, kind: str, pow_ok: bool, event: str, user: str, exp: int, bits: int, nonce: str) -> str:
    msg = f"{VERSION}|{kind}{int(pow_ok)}|{event}|{user}|{exp}|{bits}|{nonce}"
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:MAC_HEX]


def mint(key: str, kind: str, event_id: uuid.UUID, user_id: uuid.UUID, exp: int, bits: int, pow_ok: bool = False,
         nonce: str | None = None) -> str:
    if kind not in ("p", "c"):
        raise ValueError("kind must be 'p' or 'c'")
    if not 0 <= bits <= 99:
        raise ValueError("bits must be within 0..99")
    nonce = nonce or secrets.token_hex(8)
    e, u = event_id.hex, user_id.hex
    return f"{VERSION}.{kind}{int(pow_ok)}.{e}.{u}.{exp}.{bits}.{nonce}.{_mac(key, kind, pow_ok, e, u, exp, bits, nonce)}"


def check(raw: str, key: str, user_id: uuid.UUID, event_id: uuid.UUID, now: int) -> tuple[Status, Token | None]:
    m = _TOKEN.fullmatch(raw or "")
    if not m:
        return Status.FORGED, None
    kind, ok, event, user, exp, bits, nonce, mac = m.groups()
    tok = Token(raw, kind, ok == "1", event, user, int(exp), int(bits), nonce, mac)
    expected = _mac(key, kind, tok.pow_ok, event, user, tok.exp, tok.bits, nonce)
    if not hmac.compare_digest(expected, mac):  # constant time
        return Status.FORGED, None
    if event != event_id.hex or user != user_id.hex:
        return Status.WRONG_BINDING, tok
    if now > tok.exp:
        return Status.EXPIRED, tok
    return Status.OK, tok


def leading_zero_bits(digest: bytes) -> int:
    n = 0
    for byte in digest:
        if byte == 0:
            n += 8
            continue
        return n + (8 - byte.bit_length())
    return n


def solution_ok(prefix: str, solution: str, bits: int) -> bool:
    """SHA-256(utf8(prefix + ':' + solution)) has at least `bits` leading zero bits."""
    if not _SOLUTION.fullmatch(solution or ""):
        return False
    return leading_zero_bits(hashlib.sha256(f"{prefix}:{solution}".encode()).digest()) >= bits


def solve(prefix: str, bits: int, start: int = 0, limit: int = 1 << 32) -> str:
    """Reference solver (smallest nonce >= start). For tests, C's simulator and benchmarks."""
    n = start
    h = hashlib.sha256
    while n < limit:
        if leading_zero_bits(h(f"{prefix}:{n}".encode()).digest()) >= bits:
            return str(n)
        n += 1
    raise RuntimeError("no solution within limit")
