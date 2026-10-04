"""Email policy checks and soft flags.

Hard checks (registration is refused): domain allowlist, disposable-domain blocklist.
Soft flags (recorded on the identity, scored later by the risk engine, never a
rejection on their own): sequential-pattern cluster, random-looking local part.
Both soft flags are heuristics that will have false positives; that is why they only
feed a score.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Sequential pattern: >= SEQ_THRESHOLD distinct addresses with the same skeleton inside SEQ_WINDOW_S.
SEQ_THRESHOLD = 5
SEQ_WINDOW_S = 900
# A pattern needs at least this many letters. Without it, roll-number addresses
# ("2021001@", "2021002@") would all collapse to "#" and every student would look like a bot.
MIN_SKELETON_LETTERS = 3

_DIGIT_RUN = re.compile(r"\d+")

ENTROPY_MIN_LEN = 10
ENTROPY_MIN_BITS = 3.2
ENTROPY_MIN_CLASS_SWITCHES = 3


@lru_cache(maxsize=1)
def disposable_domains() -> frozenset[str]:
    out = set()
    for line in (DATA_DIR / "disposable_domains.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return frozenset(out)


def _parents(domain: str):
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        yield ".".join(parts[i:])


def is_disposable(domain: str) -> bool:
    """Matches the listed domain and any subdomain of it."""
    bad = disposable_domains()
    return any(d in bad for d in _parents(domain))


def domain_allowed(domain: str, allowlist: tuple[str, ...]) -> bool:
    """Entries: exact domain, "*.example.edu" (subdomains only), or "*" (anything).
    A bare "example.edu" does NOT admit "mail.example.edu": allow subdomains explicitly."""
    for entry in allowlist:
        if entry == "*" or entry == domain:
            return True
        if entry.startswith("*.") and domain.endswith(entry[1:]) and domain != entry[2:]:
            return True
    return False


def pattern_key(local: str, domain: str) -> str | None:
    """Digit-normalised skeleton: user1, user_2, user.37 -> "domain|user#". None if the
    address is not clusterable (too few letters or no digits)."""
    if not re.search(r"\d", local):
        return None
    skeleton = re.sub(r"[._+-]", "", local)
    letters = re.sub(r"[^a-z]", "", skeleton)
    if len(letters) < MIN_SKELETON_LETTERS:
        return None
    normalised = _DIGIT_RUN.sub("#", skeleton)
    return f"{domain}|{normalised}"


def entropy_bits_per_char(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def looks_random(local: str) -> bool:
    """Random-looking local part: long, high character entropy, and many switches between
    letters and digits ("xk2j9qpzv4mw"). A name plus a roll number ("rahul.sharma2021")
    switches once and stays below the bar."""
    s = re.sub(r"[._+-]", "", local)
    if len(s) < ENTROPY_MIN_LEN:
        return False
    classes = ["d" if ch.isdigit() else "a" for ch in s]
    switches = sum(1 for a, b in zip(classes, classes[1:]) if a != b)
    return entropy_bits_per_char(s) >= ENTROPY_MIN_BITS and switches >= ENTROPY_MIN_CLASS_SWITCHES
