"""X-Admin-Token check for the admin routes that Member B owns."""
from __future__ import annotations

import hmac

from fastapi import Header

from .errors import ApiError, ErrorCode
from .settings import get_settings


async def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_token
    if not expected:
        # Fail closed: an unset ADMIN_TOKEN must not mean "anyone is admin".
        raise ApiError(ErrorCode.FORBIDDEN, "admin API disabled: ADMIN_TOKEN is not configured")
    if not x_admin_token:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "X-Admin-Token header required")
    # compare_digest on bytes: constant time, and safe for non-ASCII input.
    if not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
        raise ApiError(ErrorCode.FORBIDDEN, "invalid admin token")
