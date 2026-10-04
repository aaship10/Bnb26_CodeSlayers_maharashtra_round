"""Gather the facts and turn them into Signal values for one person at one moment.

Identity-derived signals need that person's row in defence.identities (written when they verified their
email). A person WITHOUT a row (for example a simulator-minted identity that skipped registration) gets
neutral identity signals plus an explicit `identity_record: missing` note, never a penalty: absence of
evidence is not evidence. Header and timing signals still apply to them.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..config_schema import SignalsLayer
from ..runtime import Runtime
from . import counts, features, timing
from .asn import asn_of
from .features import Signal


async def load_identity(rt: Runtime, user_id: Any) -> dict[str, Any] | None:
    async with rt.pg.connection() as conn:
        return await (
            await conn.execute(
                """SELECT host(registration_ip) AS ip, registration_subnet AS subnet, device_id, verified_at,
                          otp_latency_ms, email_flags
                     FROM defence.identities WHERE user_id = %s""",
                (user_id,),
            )
        ).fetchone()


async def collect(
    rt: Runtime, env: str, cfg: SignalsLayer, *, event_id: str, user_id: Any, headers: Mapping[str, str], now: datetime
) -> list[Signal]:
    out: list[Signal] = [features.header_anomaly(headers)]
    out.append(features.timing_regularity(await timing.intervals_ms(rt.redis, env, event_id, str(user_id)), cfg.timing_min_samples))

    ident = await load_identity(rt, user_id)
    if ident is None:
        out.append(Signal("accounts_per_device", 0.0, {"identity_record": "missing"}))
        return out

    flags = ident["email_flags"] or {}
    if ident["device_id"]:
        out.append(features.accounts_per_device(await counts.device_accounts(rt, env, ident["device_id"])))
    if ident["ip"]:
        out.append(features.accounts_per_ip(await counts.ip_accounts(rt, env, ident["ip"])))
        asn = asn_of(ident["ip"])
        if asn:
            out.append(features.accounts_per_asn(await counts.asn_accounts(rt, env, asn[1]), asn[0]))
    if ident["subnet"]:
        out.append(features.accounts_per_subnet(await counts.subnet_accounts(rt, env, ident["subnet"])))
        out.append(features.registration_velocity(await counts.subnet_burst(rt, env, ident["subnet"], ident["verified_at"])))
    out.append(features.account_age((now - ident["verified_at"]).total_seconds()))
    out.append(features.email_pattern(flags))
    out.append(features.email_entropy(flags))
    out.append(features.otp_latency(ident["otp_latency_ms"]))
    return out
