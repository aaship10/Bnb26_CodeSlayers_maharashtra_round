"""One-time codes: generation, hashing, constant-time verification.

Honest note on what the hash buys: a 6-digit code has only 10^6 values, so a leaked
database could be brute-forced offline in milliseconds *unless* the HMAC pepper is
also secret (it lives in the environment, not the database). The real protection is
online: 10-minute expiry, 5 attempts per code, and per-email/IP request limits.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

OTP_TTL_S = 600
OTP_MAX_ATTEMPTS = 5
_FORMAT = re.compile(r"[0-9]{6}")  # ASCII digits only; fullmatch so a trailing newline is not accepted


def generate_otp() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def is_well_formed(otp: str) -> bool:
    return bool(_FORMAT.fullmatch(otp or ""))


def hash_otp(pepper: str, email_canonical: str, otp: str) -> str:
    # Binding the email stops a hash copied from one row from validating on another.
    return hmac.new(pepper.encode(), f"{email_canonical}\x00{otp}".encode(), hashlib.sha256).hexdigest()


def verify_otp(pepper: str, email_canonical: str, otp: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(pepper, email_canonical, otp), stored_hash)
