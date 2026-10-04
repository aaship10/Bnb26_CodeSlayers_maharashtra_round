"""CAPTCHA providers behind one interface. A CAPTCHA is ONE signal, never the whole defence.

MockCaptcha (default) provides NO security by itself. It exists for two audiences:
  * the browser demo: the frontend's mock widget submits the literal token "mock-captcha-ok".
    Accepted only when MOCK_CAPTCHA_UI is true (default: FD_ENV=dev only). Anyone who knows the string
    passes, so it must never be enabled where it matters.
  * Member C's simulator, to model a CAPTCHA-solving service with cost and latency. Tokens are
    signed with SIM_KEY and accepted only while SIMULATION_MODE=true:

        sim1.<user32>.<event32>.<unix_ts>.<mac32>
        mac32 = first 32 hex of HMAC-SHA256(SIM_KEY, "sim1|<user32>|<event32>|<unix_ts>")   (valid 300 s)

TurnstileCaptcha: Cloudflare Turnstile. Endpoint, parameters and test keys checked against the
official docs (developers.cloudflare.com/turnstile): POST https://challenges.cloudflare.com/turnstile/v0/siteverify
with form fields `secret` and `response` (+ optional `remoteip`); JSON reply `{success, error-codes, ...}`;
a token is single use and valid 300 s. The SECRET comes from CAPTCHA_SECRET (env), never event config.

Accessible alternative (WCAG: no cognitive-test-only gate): an organiser can waive the CAPTCHA for a
specific person, see waivers.py. Proof-of-work is non-visual and automatic, so it is not a barrier
for screen-reader users.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from typing import Protocol

import httpx

from ..settings import Settings

log = logging.getLogger("fd.captcha")

MOCK_UI_TOKEN = "mock-captcha-ok"
SIM_TOKEN_TTL_S = 300
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_TOKEN_LEN = 2048  # Turnstile's documented maximum; also bounds what we forward


class ProviderUnavailable(Exception):
    """The provider could not be asked (network, 5xx). NOT the same as 'token rejected'."""


class CaptchaProvider(Protocol):
    name: str

    async def verify(self, token: str, *, user_id: uuid.UUID, event_id: uuid.UUID, remote_ip: str | None) -> bool: ...


def mint_sim_token(sim_key: str, user_id: uuid.UUID, event_id: uuid.UUID, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    msg = f"sim1|{user_id.hex}|{event_id.hex}|{ts}"
    mac = hmac.new(sim_key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"sim1.{user_id.hex}.{event_id.hex}.{ts}.{mac}"


class MockCaptcha:
    name = "mock"

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    async def verify(self, token: str, *, user_id: uuid.UUID, event_id: uuid.UUID, remote_ip: str | None) -> bool:
        if not token or len(token) > MAX_TOKEN_LEN:
            return False
        if self._s.mock_captcha_ui and hmac.compare_digest(token.encode(), MOCK_UI_TOKEN.encode()):
            return True
        if self._s.simulation_mode and self._s.sim_key and token.startswith("sim1."):
            parts = token.split(".")
            if len(parts) != 5 or parts[1] != user_id.hex or parts[2] != event_id.hex or not parts[3].isdigit():
                return False
            if abs(int(time.time()) - int(parts[3])) > SIM_TOKEN_TTL_S:
                return False
            expected = mint_sim_token(self._s.sim_key, user_id, event_id, int(parts[3]))
            return hmac.compare_digest(expected.encode(), token.encode())
        return False


class TurnstileCaptcha:
    name = "turnstile"

    def __init__(self, secret: str, client: httpx.AsyncClient | None = None, url: str = TURNSTILE_URL) -> None:
        if not secret:
            raise ValueError("CAPTCHA_SECRET is required for the turnstile provider")
        self._secret, self._url = secret, url
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0))

    def __repr__(self) -> str:
        return "TurnstileCaptcha(secret=<hidden>)"

    async def verify(self, token: str, *, user_id: uuid.UUID, event_id: uuid.UUID, remote_ip: str | None) -> bool:
        if not token or len(token) > MAX_TOKEN_LEN:
            return False
        data = {"secret": self._secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        try:
            r = await self._client.post(self._url, data=data)
            if r.status_code >= 500:
                raise ProviderUnavailable(f"turnstile answered {r.status_code}")
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(repr(exc)) from exc
        ok = body.get("success") is True
        if not ok:
            log.info("turnstile rejected a token: %s", body.get("error-codes"))
        return ok


def build_provider(name: str, settings: Settings) -> CaptchaProvider:
    if name == "mock":
        return MockCaptcha(settings)
    if name == "turnstile":
        return TurnstileCaptcha(settings.captcha_secret)
    raise ValueError(f"unknown captcha provider {name!r}")
