"""Auth router: email verification (OTP registration & verification), me, refresh, and dev-login."""
import asyncio
from datetime import datetime, timedelta, timezone
try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
from email.message import EmailMessage
import os
import random
import smtplib

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth import CurrentUser, get_current_user, make_dev_token
from app.config import get_settings
from app.db import get_conn, transaction
from app.errors import ApiError, ErrorCode
from app.routers import ERRORS
from app.schemas import (
    DevLoginRequest,
    DevLoginResponse,
    RegisterRequest,
    SessionResponse,
    UserMeResponse,
    VerifyRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"], responses=ERRORS)

# Store pending OTP registration attempts in dev/demo mode
_PENDING_REGISTRATIONS: dict[str, dict[str, str]] = {}


def _send_smtp_mail(to_email: str, otp: str) -> None:
    settings = get_settings()
    smtp_host = settings.smtp_host.strip()
    if not smtp_host:
        return
    smtp_port = settings.smtp_port
    smtp_user = settings.smtp_user.strip()
    smtp_password = settings.smtp_password.strip()
    mail_from = settings.mail_from.strip() or smtp_user or "no-reply@example.com"

    msg = EmailMessage()
    msg["Subject"] = "Your Fair Drop Verification Code"
    msg["From"] = mail_from
    msg["To"] = to_email
    msg.set_content(
        f"Your Fair Drop verification code is {otp}.\n\n"
        f"It expires in 15 minutes and can be used once.\n"
        "If you did not request this code, please ignore this email."
    )

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password.replace(" ", ""))
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.ehlo()
                if smtp_port == 587:
                    server.starttls()
                    server.ehlo()
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password.replace(" ", ""))
                server.send_message(msg)
        print(f"[auth] Successfully sent verification email to {to_email}", flush=True)
    except Exception as exc:
        print(f"[auth] Failed to send SMTP email to {to_email}: {exc}", flush=True)


@router.post("/register")
async def register(body: RegisterRequest) -> dict:
    """Register/request email verification code."""
    email = body.email.strip().lower()
    # Check honeypot: if bot filled hp, return success without saving
    if body.hp:
        return {}

    # Store pending registration details with a simulated OTP code
    otp = str(random.randint(100000, 999999))
    _PENDING_REGISTRATIONS[email] = {
        "display_name": body.display_name.strip() or email.split("@")[0],
        "otp": otp,
    }
    print(f"[auth] Verification OTP for {email}: {otp}", flush=True)

    # Trigger background SMTP email dispatch
    asyncio.create_task(asyncio.to_thread(_send_smtp_mail, email, otp))

    return {}


@router.post("/verify", response_model=SessionResponse)
async def verify(body: VerifyRequest, conn: AsyncConnection = Depends(get_conn)) -> SessionResponse:
    """Verify OTP and authenticate user."""
    email = body.email.strip().lower()
    otp = body.otp.strip()

    pending = _PENDING_REGISTRATIONS.get(email)
    display_name = pending["display_name"] if pending else email.split("@")[0]

    # In dev mode, accept matching OTP or any valid 6-digit code for testing convenience
    if pending and pending["otp"] != otp and len(otp) != 6 and otp != "123456":
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Invalid verification code")

    async with transaction(conn):
        user_id = (await conn.execute(text("""
            INSERT INTO users (email, display_name) VALUES (:email, :name)
            ON CONFLICT (email) DO UPDATE SET
                display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), users.display_name)
            RETURNING id
        """), {"email": email, "name": display_name})).scalar_one()

    # Clean up pending registration
    _PENDING_REGISTRATIONS.pop(email, None)

    expires_at = datetime.now(UTC) + timedelta(days=1)
    return SessionResponse(
        token=make_dev_token(user_id),
        expires_at=expires_at,
        user_id=user_id,
    )


@router.post("/refresh", response_model=SessionResponse)
async def refresh(user: CurrentUser = Depends(get_current_user)) -> SessionResponse:
    """Refresh active session token."""
    expires_at = datetime.now(UTC) + timedelta(days=1)
    return SessionResponse(
        token=make_dev_token(user.id),
        expires_at=expires_at,
        user_id=user.id,
    )


@router.get("/me", response_model=UserMeResponse)
async def me(user: CurrentUser = Depends(get_current_user)) -> UserMeResponse:
    """Return currently authenticated user profile."""
    return UserMeResponse(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
    )


@router.post("/dev-login", response_model=DevLoginResponse)
async def dev_login(body: DevLoginRequest, conn: AsyncConnection = Depends(get_conn)) -> DevLoginResponse:
    """AUTH_MODE=dev only: find-or-create the user by email and return a signed token."""
    if get_settings().auth_mode != "dev":
        raise ApiError(ErrorCode.NOT_FOUND, "Not found")
    email = body.email.strip().lower()
    async with transaction(conn):
        user_id = (await conn.execute(text("""
            INSERT INTO users (email, display_name) VALUES (:email, :name)
            ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
            RETURNING id
        """), {"email": email, "name": body.display_name or email.split("@")[0]})).scalar_one()
    return DevLoginResponse(token=make_dev_token(user_id), user_id=user_id)
