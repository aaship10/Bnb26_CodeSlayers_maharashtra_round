"""/auth/* routes and the simulation token minter."""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field, field_validator

from .. import metrics
from ..admin_auth import require_admin
from ..clientip import resolve_client
from ..errors import ApiError, ErrorCode
from ..ratelimit.errors import rate_limited
from ..runtime import Runtime, get_runtime
from ..settings import Settings, get_settings
from ..simheaders import sim_authorized, warn_throttled
from ..timeutil import iso_z, utcnow
from . import emailchecks, limits, tokens
from .deps import get_current_user, token_claims
from .emailcanon import CanonicalEmail, EmailError, canonicalize
from .mail import otp_message
from .otp import OTP_TTL_S, is_well_formed
from .service import RequestInfo, start_registration, verify_code

log = logging.getLogger("fd.identity")
router = APIRouter(prefix="/auth", tags=["auth"])
sim_router = APIRouter(prefix="/admin/sim", tags=["simulation"], dependencies=[Depends(require_admin)])


class RegisterBody(BaseModel):
    email: str = Field(max_length=254)
    display_name: str = Field(min_length=1, max_length=80)
    hp: str = Field(default="", max_length=500)  # honeypot: invisible field, humans leave it empty

    @field_validator("display_name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = v.strip()
        if not v or any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise ValueError("display_name must be non-empty text without control characters")
        return v


class VerifyBody(BaseModel):
    email: str = Field(max_length=254)
    otp: str = Field(max_length=16)


class SimTokenBody(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=10_000)
    ttl_s: int = Field(default=3600, ge=60, le=86_400)


# ------------------------------------------------------------------ helpers
def _need_identity(s: Settings) -> None:
    if not s.identity_enabled:
        raise ApiError(ErrorCode.INTERNAL, "identity is not configured: set JWT_SECRET", status=503)


def _validation(reason: str, field: str = "email") -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_ERROR, "request validation failed", details={"errors": [{"loc": ["body", field], "reason": reason}]}
    )


def _canon(raw: str) -> CanonicalEmail:
    try:
        return canonicalize(raw)
    except EmailError as exc:
        raise _validation(exc.reason) from None


def _request_info(request: Request, now: datetime) -> RequestInfo:
    st = request.state
    ip, subnet = getattr(st, "client_ip", None), getattr(st, "client_subnet", None)
    if ip is None:  # middleware not installed (unit tests / foreign app): resolve here
        peer = request.client.host if request.client else None
        info = resolve_client(peer, request.headers, get_settings().trusted_proxies, sim_authorized(request.headers))
        ip, subnet = info.ip, info.subnet
    device: str | None = None
    raw_dev = request.headers.get("x-device-id")
    if raw_dev:
        try:  # only well-formed UUIDs: the header is client-chosen, so never trust its shape
            device = str(uuid.UUID(raw_dev))
        except ValueError:
            device = None
    ua = request.headers.get("user-agent")
    return RequestInfo(ip, subnet, device, hashlib.sha256(ua.encode()).hexdigest()[:16] if ua else None, now)


async def _send_otp(rt: Runtime, to: str, otp: str) -> None:
    try:
        await rt.mailer.send(otp_message(to, otp, OTP_TTL_S // 60))
    except Exception:  # noqa: BLE001
        # Runs after the response: the client already got 202. The user can request a new code.
        log.exception("failed to send OTP email")


def _accepted(now: datetime, otp: str | None) -> dict:
    body: dict = {"status": "otp_sent", "expires_in_s": OTP_TTL_S, "server_now": iso_z(now)}
    if otp is not None:
        body["otp"] = otp
    return body


# ------------------------------------------------------------------- routes
@router.post("/register", status_code=202)
async def register(body: RegisterBody, request: Request, background: BackgroundTasks) -> dict:
    s = get_settings()
    _need_identity(s)
    now = utcnow()

    if body.hp:
        # Honeypot filled: a human cannot see this field. Answer exactly like a success
        # (teaching a bot what failed helps it adapt) but create nothing and send nothing.
        warn_throttled("honeypot", "registration honeypot hit")
        return _accepted(now, None)

    canon = _canon(body.email)
    if not emailchecks.domain_allowed(canon.domain, s.allowed_email_domains):
        raise _validation("domain_not_allowed")
    if emailchecks.is_disposable(canon.domain):
        raise _validation("disposable_domain")

    info = _request_info(request, now)
    rt = await get_runtime()
    res = await rt.limiter.check_policy(
        limits.limits_for("register", limits.REGISTER, info.ip, info.subnet, info.device_id, canon.canonical), "local"
    )
    if not res.allowed:
        metrics.RATE_LIMITED.labels("register", res.scope or "unknown").inc()
        raise rate_limited(res)

    reg = await start_registration(rt, s.otp_pepper, canon, body.display_name, info)
    background.add_task(_send_otp, rt, canon.original, reg.otp)
    # The code leaves the server in the response ONLY for an authenticated simulation client.
    return _accepted(now, reg.otp if sim_authorized(request.headers) else None)


@router.post("/verify")
async def verify(body: VerifyBody, request: Request) -> dict:
    s = get_settings()
    _need_identity(s)
    now = utcnow()
    canon = _canon(body.email)
    if not is_well_formed(body.otp):
        raise _validation("otp_must_be_6_digits", "otp")  # not charged as an attempt

    info = _request_info(request, now)
    rt = await get_runtime()
    res = await rt.limiter.check_policy(
        limits.limits_for("verify", limits.VERIFY, info.ip, info.subnet, info.device_id, canon.canonical), "local"
    )
    if not res.allowed:
        metrics.RATE_LIMITED.labels("verify", res.scope or "unknown").inc()
        raise rate_limited(res)

    v = await verify_code(rt, s.otp_pepper, canon, body.otp, now)
    token, exp = tokens.mint(v.user_id, email=v.email, name=v.display_name)
    return {"token": token, "expires_at": iso_z(datetime.fromtimestamp(exp, tz=now.tzinfo)), "user_id": str(v.user_id), "server_now": iso_z(now)}


@router.post("/refresh")
async def refresh(request: Request) -> dict:
    s = get_settings()
    _need_identity(s)
    now = utcnow()
    c = token_claims(request, grace_s=s.refresh_grace_s)
    # auth_time is carried over so refreshing cannot extend a session past SESSION_MAX_S.
    token, exp = tokens.mint(c.user_id, email=c.email, name=c.name, auth_time=c.auth_time)
    return {"token": token, "expires_at": iso_z(datetime.fromtimestamp(exp, tz=now.tzinfo)), "user_id": str(c.user_id), "server_now": iso_z(now)}


@router.get("/me")
async def me(request: Request, user: dict = Depends(get_current_user)) -> dict:
    return {
        "user_id": str(user["id"]),
        # The frontend's schema wants strings; sim/dev identities have no profile, so "" not null.
        "email": user["email"] or "",
        "display_name": user["display_name"] or "",
        "auth": user["via"],
        "server_now": iso_z(utcnow()),
    }


@sim_router.post("/tokens")
async def mint_sim_tokens(body: SimTokenBody) -> dict:
    s = get_settings()
    if not s.simulation_mode:
        raise ApiError(ErrorCode.NOT_FOUND, "not found")  # does not exist outside simulation mode
    _need_identity(s)
    out: dict[str, str] = {}
    exp = 0
    for uid in body.user_ids:
        out[str(uid)], exp = tokens.mint(uid, ttl_s=body.ttl_s)
    return {"tokens": out, "expires_at": iso_z(datetime.fromtimestamp(exp, tz=utcnow().tzinfo)), "server_now": iso_z(utcnow())}
