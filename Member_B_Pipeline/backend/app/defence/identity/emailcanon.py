"""Email canonicalisation: one real mailbox must map to exactly one identity.

Rules, and why each is where it is:
* ASCII only. Fullwidth / homoglyph / IDN spellings are rejected rather than
  "normalised": silent normalisation is how look-alike duplicates sneak in.
* lower-case everything (domains are case-insensitive; for local parts we accept the
  universal practice).
* strip "+tag" from the local part. Valid for essentially every mail system a
  college would use, and it is the cheapest alias attack there is.
* Dots in the local part are collapsed ONLY for Gmail/Googlemail, where Google
  documents them as insignificant. At an institutional domain john.smith and
  johnsmith may be two different students, so collapsing there would lock out
  real people. Consequence (documented, not hidden): dot tricks at other domains
  are not caught by canonicalisation; the sequential-pattern flag and the
  email allowlist limit the damage.
* googlemail.com is folded into gmail.com (same mailbox).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_FOLD_DOMAINS = {"googlemail.com": "gmail.com"}
_DOT_INSENSITIVE = {"gmail.com"}
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_FULL = re.compile(r"^[a-z0-9._+-]+$")
_LOCAL_BASE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")


class EmailError(ValueError):
    """`reason` is a stable machine-readable code returned in VALIDATION_ERROR details."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CanonicalEmail:
    original: str  # trimmed, as typed
    local: str  # canonical local part
    domain: str  # canonical domain
    canonical: str  # local@domain, the identity key


def canonicalize(raw: str) -> CanonicalEmail:
    s = (raw or "").strip()
    if not s:
        raise EmailError("empty")
    if len(s) > 254:
        raise EmailError("too_long")
    if not s.isascii():
        raise EmailError("not_ascii")
    if s.count("@") != 1:
        raise EmailError("malformed")
    local, domain = s.lower().split("@")

    labels = domain.split(".")
    if len(labels) < 2 or any(not _LABEL.match(lbl) for lbl in labels) or labels[-1].isdigit():
        raise EmailError("bad_domain")

    if not local or len(local) > 64 or not _LOCAL_FULL.match(local):
        raise EmailError("bad_local")
    base = local.split("+", 1)[0]
    if not base:
        raise EmailError("empty_after_tag")
    if not _LOCAL_BASE.match(base) or ".." in base:
        raise EmailError("bad_local")

    domain = _FOLD_DOMAINS.get(domain, domain)
    if domain in _DOT_INSENSITIVE:
        base = base.replace(".", "")
        if not base:
            raise EmailError("bad_local")
    return CanonicalEmail(original=s, local=base, domain=domain, canonical=f"{base}@{domain}")
