"""Registration / OTP verification against Postgres.

Facts that influence weights (who registered from where, on which device, how fast)
are written to defence.identities. Redis never holds them.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..errors import ApiError, ErrorCode
from ..runtime import Runtime
from . import emailchecks
from .emailcanon import CanonicalEmail
from .otp import OTP_MAX_ATTEMPTS, OTP_TTL_S, generate_otp, hash_otp, verify_otp


@dataclass(frozen=True)
class RequestInfo:
    ip: str
    subnet: str
    device_id: str | None
    ua_hash: str | None
    now: datetime


@dataclass(frozen=True)
class Registered:
    otp: str
    existing_identity: bool
    flags: dict[str, Any]


@dataclass(frozen=True)
class Verified:
    user_id: uuid.UUID
    email: str
    display_name: str
    is_new: bool
    otp_latency_ms: int


def _invalid_code() -> ApiError:
    # One message for unknown email / expired / too many attempts / wrong code:
    # the caller learns nothing about which accounts or pending codes exist.
    return ApiError(ErrorCode.UNAUTHENTICATED, "invalid or expired code")


async def start_registration(
    rt: Runtime, pepper: str, canon: CanonicalEmail, display_name: str, req: RequestInfo
) -> Registered:
    pattern = emailchecks.pattern_key(canon.local, canon.domain)
    otp = generate_otp()
    window_start = req.now - timedelta(seconds=emailchecks.SEQ_WINDOW_S)

    async with rt.pg.connection() as conn:
        existing = await (
            await conn.execute("SELECT 1 FROM defence.identities WHERE email_canonical = %s", (canon.canonical,))
        ).fetchone()

        cluster = 1
        if pattern is not None:
            # Distinct OTHER addresses with the same skeleton in the window, plus this one.
            row = await (
                await conn.execute(
                    """SELECT count(DISTINCT email_canonical) AS n FROM (
                           SELECT email_canonical FROM defence.identities
                            WHERE email_pattern = %s AND registered_at > %s
                           UNION ALL
                           SELECT email_canonical FROM defence.pending_registrations
                            WHERE email_pattern = %s AND requested_at > %s) x
                        WHERE email_canonical <> %s""",
                    (pattern, window_start, pattern, window_start, canon.canonical),
                )
            ).fetchone()
            cluster = int(row["n"]) + 1

        flags = {
            "sequential_pattern": pattern is not None and cluster >= emailchecks.SEQ_THRESHOLD,
            "pattern_cluster_size": cluster if pattern is not None else 0,
            "random_local_part": emailchecks.looks_random(canon.local),
        }
        await conn.execute(
            """INSERT INTO defence.pending_registrations
                   (email_canonical, email_original, email_domain, email_pattern, email_flags, display_name,
                    otp_hash, attempts, requested_at, expires_at, registration_ip, registration_subnet,
                    device_id, user_agent_hash)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,0,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (email_canonical) DO UPDATE SET
                   email_original = EXCLUDED.email_original, email_pattern = EXCLUDED.email_pattern,
                   email_flags = EXCLUDED.email_flags, display_name = EXCLUDED.display_name,
                   otp_hash = EXCLUDED.otp_hash, attempts = 0, requested_at = EXCLUDED.requested_at,
                   expires_at = EXCLUDED.expires_at, registration_ip = EXCLUDED.registration_ip,
                   registration_subnet = EXCLUDED.registration_subnet, device_id = EXCLUDED.device_id,
                   user_agent_hash = EXCLUDED.user_agent_hash""",
            (
                canon.canonical, canon.original, canon.domain, pattern, json.dumps(flags), display_name,
                hash_otp(pepper, canon.canonical, otp), req.now, req.now + timedelta(seconds=OTP_TTL_S),
                req.ip, req.subnet, req.device_id, req.ua_hash,
            ),
        )
    return Registered(otp=otp, existing_identity=existing is not None, flags=flags)


async def verify_code(rt: Runtime, pepper: str, canon: CanonicalEmail, otp: str, now: datetime) -> Verified:
    outcome: Verified | None = None
    async with rt.pg.connection() as conn:
        # The attempt is charged BEFORE the code is compared, in the same statement that
        # checks the limits. The row lock this takes also serialises concurrent verifies
        # of one address, so a single code can be consumed exactly once.
        cur = await conn.execute(
            """UPDATE defence.pending_registrations SET attempts = attempts + 1
                WHERE email_canonical = %s AND expires_at > %s AND attempts < %s
            RETURNING *""",
            (canon.canonical, now, OTP_MAX_ATTEMPTS),
        )
        pending = await cur.fetchone()
        if pending is not None and verify_otp(pepper, canon.canonical, otp, pending["otp_hash"]):
            await conn.execute("DELETE FROM defence.pending_registrations WHERE email_canonical = %s", (canon.canonical,))
            outcome = await _complete(conn, canon, pending, now)
    # Raise only after leaving the block: raising inside would roll back the attempt counter.
    if outcome is None:
        raise _invalid_code()
    return outcome


async def _complete(conn: Any, canon: CanonicalEmail, pending: dict[str, Any], now: datetime) -> Verified:
    latency_ms = max(0, int((now - pending["requested_at"]).total_seconds() * 1000))
    ident = await (
        await conn.execute(
            "SELECT user_id, display_name FROM defence.identities WHERE email_canonical = %s", (canon.canonical,)
        )
    ).fetchone()
    if ident is not None:  # returning user: this is a login, nothing is created
        return Verified(ident["user_id"], canon.canonical, ident["display_name"], False, latency_ms)

    # A's users table. We supply the id ourselves so this works whether or not A's column
    # has a default; ON CONFLICT covers an account created through another path (R9).
    new_id = uuid.uuid4()
    row = await (
        await conn.execute(
            """INSERT INTO public.users (id, email, display_name) VALUES (%s, %s, %s)
               ON CONFLICT (email) DO NOTHING RETURNING id""",
            (new_id, canon.canonical, pending["display_name"]),
        )
    ).fetchone()
    user_id = row["id"] if row else (
        await (await conn.execute("SELECT id FROM public.users WHERE email = %s", (canon.canonical,))).fetchone()
    )["id"]

    await conn.execute(
        """INSERT INTO defence.identities
               (user_id, email_canonical, email_original, email_domain, email_pattern, email_flags, display_name,
                registration_ip, registration_subnet, device_id, user_agent_hash,
                registered_at, verified_at, otp_latency_ms)
           VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (email_canonical) DO NOTHING""",
        (
            user_id, canon.canonical, pending["email_original"], pending["email_domain"], pending["email_pattern"],
            json.dumps(pending["email_flags"]), pending["display_name"], pending["registration_ip"],
            pending["registration_subnet"], pending["device_id"], pending["user_agent_hash"],
            pending["requested_at"], now, latency_ms,
        ),
    )
    return Verified(user_id, canon.canonical, pending["display_name"], True, latency_ms)
