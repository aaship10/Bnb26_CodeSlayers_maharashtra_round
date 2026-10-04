"""Environment-driven settings shared by every defence module.

Per-event behaviour lives in events.config.defences (see config_schema.py);
this module only holds deployment-level facts and secrets.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be within [{lo}, {hi}], got {v}")
    return v


def _derive(secret: str, label: bytes) -> str:
    """Domain-separated sub-key: a token minted for one purpose can never be valid for another."""
    if not secret:
        return ""
    import hashlib
    import hmac

    return hmac.new(secret.encode(), label, hashlib.sha256).hexdigest()


def _smtp_password() -> str:
    pw = os.environ.get("SMTP_PASSWORD", "").strip()
    host = os.environ.get("SMTP_HOST", "").strip().lower()
    if host.endswith(("gmail.com", "googlemail.com")):
        # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces are not part of it.
        pw = "".join(pw.split())
    return pw


def _smtp_tls() -> str:
    raw = os.environ.get("SMTP_TLS", "").strip().lower()
    if raw not in ("", "starttls", "tls", "none"):
        raise ValueError(f"SMTP_TLS must be starttls, tls or none, got {raw!r}")
    if raw:
        return raw
    port = os.environ.get("SMTP_PORT", "").strip()
    return "tls" if port == "465" else "starttls" if os.environ.get("SMTP_USER", "").strip() else "none"


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(p.strip() for p in os.environ.get(name, default).split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    env: str
    admin_token: str
    auth_mode: Literal["dev", "jwt"]
    simulation_mode: bool
    sim_key: str
    # Only these peers may set X-Forwarded-For / X-Real-IP. Empty = trust nobody.
    trusted_proxies: tuple[IPNetwork, ...]
    jwt_secret: str
    allowed_email_domains: tuple[str, ...]  # ("*",) = any domain (disposable blocklist still applies)
    redis_url: str
    database_url: str
    # --- identity (stage 2)
    otp_pepper: str  # HMAC key for stored OTP hashes; falls back to jwt_secret
    jwt_ttl_s: int  # access-token lifetime
    refresh_grace_s: int  # an expired token may still be refreshed this long after exp...
    session_max_s: int  # ...but never beyond this age since the OTP login (auth_time)
    smtp_host: str  # empty = write messages to outbox_dir instead of sending
    smtp_port: int
    smtp_user: str
    smtp_password: str  # secret: never logged, never in event config
    smtp_tls: str  # "starttls" (port 587) | "tls" (implicit, port 465) | "none"
    mail_from: str
    # --- challenges (stage 4)
    pow_secret: str  # HMAC key for challenge tokens; derived from JWT_SECRET unless POW_SECRET is set
    captcha_secret: str  # provider secret key (Turnstile); env only, never in event config
    mock_captcha_ui: bool  # accept the frontend mock widget's token; NEVER enable outside local dev
    outbox_dir: str
    auto_migrate: bool  # run defence Alembic migrations at startup (advisory-locked)

    @property
    def identity_enabled(self) -> bool:
        return bool(self.jwt_secret) and bool(self.otp_pepper)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    auth_mode = os.environ.get("AUTH_MODE", "dev").strip().lower()
    if auth_mode not in ("dev", "jwt"):
        raise ValueError(f"AUTH_MODE must be 'dev' or 'jwt', got {auth_mode!r}")

    simulation_mode = _bool("SIMULATION_MODE", False)
    sim_key = os.environ.get("SIM_KEY", "")
    if simulation_mode and not sim_key:
        # An empty key would let any request that sends an empty X-Sim-Key through.
        raise ValueError("SIMULATION_MODE=true requires a non-empty SIM_KEY")

    jwt_secret = os.environ.get("JWT_SECRET", "")
    if auth_mode == "jwt" and len(jwt_secret) < 32:
        raise ValueError("AUTH_MODE=jwt requires JWT_SECRET of at least 32 characters")

    try:
        proxies = tuple(ipaddress.ip_network(p, strict=False) for p in _csv("TRUSTED_PROXIES"))
    except ValueError as exc:
        raise ValueError(f"TRUSTED_PROXIES must be a comma-separated list of CIDRs: {exc}") from exc

    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    if os.environ.get("SMTP_USER", "").strip() and _smtp_tls() == "none" and smtp_host not in ("", "localhost", "127.0.0.1", "::1"):
        # A password over an unencrypted connection to a remote server is never acceptable.
        raise ValueError("SMTP_USER is set but SMTP_TLS=none for a non-local SMTP_HOST: refusing to send credentials in clear")
    if bool(os.environ.get("SMTP_USER", "").strip()) != bool(_smtp_password()):
        raise ValueError("SMTP_USER and SMTP_PASSWORD must be set together")

    return Settings(
        env=os.environ.get("FD_ENV", "dev"),
        admin_token=os.environ.get("ADMIN_TOKEN", ""),
        auth_mode=auth_mode,  # type: ignore[arg-type]
        simulation_mode=simulation_mode,
        sim_key=sim_key,
        trusted_proxies=proxies,
        jwt_secret=jwt_secret,
        allowed_email_domains=tuple(d.lower() for d in _csv("ALLOWED_EMAIL_DOMAINS", "example-college.edu")),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        database_url=os.environ.get("DATABASE_URL", ""),
        otp_pepper=os.environ.get("OTP_PEPPER", "") or jwt_secret,
        jwt_ttl_s=_int("JWT_TTL_S", 1800, 60, 86400),
        refresh_grace_s=_int("JWT_REFRESH_GRACE_S", 6 * 3600, 0, 7 * 86400),
        session_max_s=_int("JWT_SESSION_MAX_S", 24 * 3600, 60, 30 * 86400),
        smtp_host=os.environ.get("SMTP_HOST", "").strip(),
        smtp_port=_int("SMTP_PORT", 1025, 1, 65535),
        smtp_user=os.environ.get("SMTP_USER", "").strip(),
        smtp_password=_smtp_password(),
        smtp_tls=_smtp_tls(),
        mail_from=os.environ.get("MAIL_FROM", "fairdrop@example-college.edu"),
        outbox_dir=os.environ.get("OUTBOX_DIR", ".local/outbox"),
        pow_secret=os.environ.get("POW_SECRET", "").strip() or _derive(jwt_secret, b"fairdrop/pow/v1"),
        captcha_secret=os.environ.get("CAPTCHA_SECRET", "").strip(),
        mock_captcha_ui=_bool("MOCK_CAPTCHA_UI", os.environ.get("FD_ENV", "dev") == "dev"),
        auto_migrate=_bool("DEFENCE_AUTO_MIGRATE", True),
    )
