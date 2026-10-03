"""Identity resolution. `get_current_user` is the ONLY place identity is resolved.

AUTH_MODE=dev (this module):
  * `X-User-Id: <uuid>` header (lets the simulator act as 50k users), or
  * `Authorization: Bearer <token>` from POST /auth/dev-login.
  The user must exist in `users`.

AUTH_MODE=jwt: Member B swaps the dependency without editing this file, from a
plugin's setup(app):

    app.dependency_overrides[get_current_user] = b_get_current_user

B's function must return a `CurrentUser` (or raise ApiError UNAUTHENTICATED).
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import get_settings
from app.db import get_conn
from app.errors import ApiError, ErrorCode


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    email: str
    display_name: str
    is_admin: bool


def _sign(user_id: UUID) -> str:
    key = get_settings().dev_auth_secret.encode()
    return hmac.new(key, str(user_id).encode(), hashlib.sha256).hexdigest()


def make_dev_token(user_id: UUID) -> str:
    return f"dev.{user_id}.{_sign(user_id)}"


def _parse_dev_token(token: str) -> UUID:
    try:
        prefix, uid, mac = token.split(".")
        user_id = UUID(uid)
    except ValueError:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Malformed token") from None
    if prefix != "dev" or not hmac.compare_digest(mac, _sign(user_id)):
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Invalid token")
    return user_id


async def load_user(conn: AsyncConnection, user_id: UUID) -> CurrentUser | None:
    row = (await conn.execute(
        text("SELECT id, email, display_name, is_admin FROM users WHERE id = :id"), {"id": user_id}
    )).first()
    return CurrentUser(row.id, row.email, row.display_name, row.is_admin) if row else None


async def get_current_user(
    conn: AsyncConnection = Depends(get_conn),
    x_user_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    settings = get_settings()
    if settings.auth_mode != "dev":
        # Reached only if Member B's override is not installed.
        raise ApiError(ErrorCode.UNAUTHENTICATED,
                       "AUTH_MODE=jwt but no auth provider plugin is installed")
    if authorization and authorization.lower().startswith("bearer "):
        user_id = _parse_dev_token(authorization[7:].strip())
    elif x_user_id:
        try:
            user_id = UUID(x_user_id)
        except ValueError:
            raise ApiError(ErrorCode.UNAUTHENTICATED, "X-User-Id is not a UUID") from None
    else:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Missing credentials (X-User-Id or Bearer token)")
    user = await load_user(conn, user_id)
    if user is None:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Unknown user")
    return user


async def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Admin endpoints: shared secret in X-Admin-Token (constant-time compare)."""
    if not x_admin_token:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Missing X-Admin-Token")
    if not hmac.compare_digest(x_admin_token.encode(), get_settings().admin_token.encode()):
        raise ApiError(ErrorCode.FORBIDDEN, "Invalid admin token")
