"""Dev-only login. Member B's real OTP/JWT flow replaces this in AUTH_MODE=jwt."""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth import make_dev_token
from app.config import get_settings
from app.db import get_conn, transaction
from app.errors import ApiError, ErrorCode
from app.routers import ERRORS
from app.schemas import DevLoginRequest, DevLoginResponse

router = APIRouter(prefix="/auth", tags=["auth"], responses=ERRORS)


@router.post("/dev-login", response_model=DevLoginResponse)
async def dev_login(body: DevLoginRequest, conn: AsyncConnection = Depends(get_conn)) -> DevLoginResponse:
    """AUTH_MODE=dev only: find-or-create the user by email and return a signed token
    for `Authorization: Bearer <token>`."""
    if get_settings().auth_mode != "dev":
        raise ApiError(ErrorCode.NOT_FOUND, "Not found")
    email = body.email.strip().lower()
    async with transaction(conn):
        # The no-op DO UPDATE makes RETURNING yield the existing row on conflict.
        user_id = (await conn.execute(text("""
            INSERT INTO users (email, display_name) VALUES (:email, :name)
            ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
            RETURNING id
        """), {"email": email, "name": body.display_name or email.split("@")[0]})).scalar_one()
    return DevLoginResponse(token=make_dev_token(user_id), user_id=user_id)
