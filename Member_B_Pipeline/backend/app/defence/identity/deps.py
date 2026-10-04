"""get_current_user: THE identity dependency. Swapping A's dev stub for this function
is the whole AUTH_MODE switch (see docs/INTERFACE_REQUESTS_B.md R6).

Returns a plain dict {id: UUID, email, display_name, via} so it is a drop-in for the
dict-shaped user of the stub. No database access: in jwt mode the identity is in the
signed token, which is what lets any replica authenticate without a lookup.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request

from ..errors import ApiError, ErrorCode
from ..settings import get_settings
from ..simheaders import sim_authorized, warn_throttled
from .tokens import Claims, TokenError, decode


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def _uuid_header(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "X-User-Id is not a UUID") from None


def token_claims(request: Request, grace_s: int = 0) -> Claims:
    token = bearer_token(request)
    if token is None:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "missing bearer token", details={"reason": "missing"})
    try:
        return decode(token, grace_s=grace_s)
    except TokenError as exc:
        # `reason` lets the frontend tell "refresh me" (expired) from "log in again" (invalid).
        raise ApiError(ErrorCode.UNAUTHENTICATED, f"token {exc.reason}", details={"reason": exc.reason}) from None


def peek_user_id(request: Request) -> uuid.UUID | None:
    """Best-effort identity for rate limiting: never raises, never touches the database.
    Same trust rules as get_current_user (X-User-Id only in dev mode or with a sim key)."""
    s = get_settings()
    xuid = request.headers.get("x-user-id")
    try:
        if s.auth_mode == "dev":
            return uuid.UUID(xuid) if xuid else None
        if xuid and sim_authorized(request.headers):
            return uuid.UUID(xuid)
        token = bearer_token(request)
        return decode(token).user_id if token else None
    except (ValueError, TokenError):
        return None


async def get_current_user(request: Request) -> dict[str, Any]:
    s = get_settings()
    xuid = request.headers.get("x-user-id")

    if s.auth_mode == "dev":
        if not xuid:
            raise ApiError(ErrorCode.UNAUTHENTICATED, "X-User-Id header required (AUTH_MODE=dev)")
        user = {"id": _uuid_header(xuid), "email": None, "display_name": None, "via": "dev"}
    else:
        user = None
        if xuid:
            if sim_authorized(request.headers):
                user = {"id": _uuid_header(xuid), "email": None, "display_name": None, "via": "sim"}
            else:
                # Identity headers must never be believed without the simulation key.
                warn_throttled("xuid-ignored", "X-User-Id ignored: not in simulation mode / bad X-Sim-Key")
        if user is None:
            c = token_claims(request)
            user = {"id": c.user_id, "email": c.email, "display_name": c.name, "via": "jwt"}

    request.state.user_id = user["id"]
    return user
