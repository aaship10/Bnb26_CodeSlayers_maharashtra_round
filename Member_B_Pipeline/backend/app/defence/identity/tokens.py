"""Stateless HS256 session tokens (any replica can verify without touching the database).

Lifetime model: short access token (JWT_TTL_S, default 30 min) so a stolen token dies
quickly, plus POST /auth/refresh. A token that expired less than REFRESH_GRACE_S ago
may still be refreshed (a sleeping laptop / backgrounded tab must not force a new OTP
mid-drop), but never beyond SESSION_MAX_S since the OTP login (auth_time).
Limitation, stated plainly: stateless tokens cannot be revoked before they expire.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt

from ..settings import get_settings

ISSUER = "fairdrop"
ALGORITHM = "HS256"
_LEEWAY_S = 5


class TokenError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "invalid" | "expired" | "session_too_old"


@dataclass(frozen=True)
class Claims:
    user_id: uuid.UUID
    email: str | None
    name: str | None
    iat: int
    exp: int
    auth_time: int


def mint(
    user_id: uuid.UUID | str,
    *,
    email: str | None = None,
    name: str | None = None,
    auth_time: int | None = None,
    ttl_s: int | None = None,
    now: int | None = None,
) -> tuple[str, int]:
    """Returns (token, exp_epoch_seconds). `now` is injectable for tests."""
    s = get_settings()
    if not s.jwt_secret:
        raise RuntimeError("JWT_SECRET is not configured")
    now = int(time.time()) if now is None else now
    exp = now + (ttl_s if ttl_s is not None else s.jwt_ttl_s)
    payload: dict = {
        "iss": ISSUER,
        "sub": str(user_id),
        "iat": now,
        "exp": exp,
        "auth_time": auth_time if auth_time is not None else now,
    }
    if email:
        payload["email"] = email
    if name:
        payload["name"] = name
    return jwt.encode(payload, s.jwt_secret, algorithm=ALGORITHM), exp


def decode(token: str, *, grace_s: int = 0, now: int | None = None) -> Claims:
    """Verify signature, issuer, required claims and expiry (own clock check so tests can
    inject time). `algorithms` is pinned: "alg: none" and algorithm-confusion tokens fail."""
    s = get_settings()
    now = int(time.time()) if now is None else now
    try:
        p = jwt.decode(
            token,
            s.jwt_secret,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss"], "verify_exp": False, "verify_iat": False},
        )
        user_id = uuid.UUID(str(p["sub"]))
        iat, exp = int(p["iat"]), int(p["exp"])
        auth_time = int(p.get("auth_time", iat))
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise TokenError("invalid") from None
    if iat > now + _LEEWAY_S:
        raise TokenError("invalid")  # minted in the future: forged or badly skewed
    if now > exp + _LEEWAY_S + grace_s:
        raise TokenError("expired")
    if grace_s and now > auth_time + s.session_max_s:
        raise TokenError("session_too_old")
    return Claims(user_id, p.get("email"), p.get("name"), iat, exp, auth_time)
