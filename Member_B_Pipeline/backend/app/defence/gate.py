"""entry_gate: the single function A calls before creating an entry.

    config (cached, hot-reloadable)
      -> signals + risk score (if the signals and risk layers are on)
      -> which challenges does this person owe?   pow/captcha in mode "always" always; in mode "risk"
                                                  only when score >= risk.thresholds.challenge
      -> verify X-Challenge-Id / X-Challenge-Solution if present
           forged token  -> REJECT        (hard evidence of tampering: the ONLY reject; never a score)
           bad / expired / replayed / someone else's -> a fresh CHALLENGE, never a lockout
      -> something outstanding -> CHALLENGE (PoW first: cheap for us; then CAPTCHA)
      -> all satisfied -> ALLOW with weight 1.0 / 0.5 / 0.25 from the risk band, risk breakdown attached

Every decision, including CHALLENGE and REJECT (which create no entry), goes to the decision log without
blocking the request. A has already run the already-entered fast path, so a repeat /enter never gets here.

PoW and CAPTCHA chain statelessly: the CAPTCHA challenge carries a signed "PoW already passed" bit.
Risk is re-evaluated on every call, so the weight reflects the person's state at the moment of entry.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from redis.exceptions import RedisError

from . import metrics
from .captcha.providers import CaptchaProvider, ProviderUnavailable, build_provider
from .captcha.waivers import is_waived
from .config_schema import DefencesConfig
from .config_source import get_defences
from .contracts import Action, CaptchaParams, Challenge, GateContext, GateDecision, PowParams
from .errors import ApiError, ErrorCode
from .pow import protocol
from .pow.adaptive import difficulty
from .pow.load import record_and_get
from .risk.engine import Risk, assess
from .runtime import Runtime, get_runtime
from .settings import Settings, get_settings
from .signals.collect import collect

log = logging.getLogger("fd.gate")
_providers: dict[str, CaptchaProvider] = {}


def _field(obj: Any, name: str) -> Any:
    return obj[name] if isinstance(obj, dict) else getattr(obj, name)


def _as_uuid(v: Any) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def _unavailable(msg: str, scope: str) -> ApiError:
    return ApiError(ErrorCode.INTERNAL, msg, status=503, headers={"Retry-After": "3"},
                    details={"retry_after_ms": 3000, "scope": scope})


def _provider(cfg: DefencesConfig, s: Settings) -> CaptchaProvider:
    name = cfg.layers.captcha.provider
    if name not in _providers:
        try:
            _providers[name] = build_provider(name, s)
        except ValueError as exc:  # e.g. turnstile without CAPTCHA_SECRET: a deployment error, say so
            log.error("captcha provider %s is misconfigured: %s", name, exc)
            raise _unavailable("verification service is not configured", "captcha") from None
    return _providers[name]


def _issue(cfg: DefencesConfig, s: Settings, kind: str, bits: int, pow_ok: bool, event_id: uuid.UUID,
           user_id: uuid.UUID, now: int) -> Challenge:
    ttl = cfg.layers.pow.ttl_s * (1 if kind == "p" else 2)  # a human needs longer for a CAPTCHA
    exp = now + ttl
    token = protocol.mint(s.pow_secret, kind, event_id, user_id, exp, bits, pow_ok)
    metrics.CHALLENGES_ISSUED.labels("pow" if kind == "p" else "captcha").inc()
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
    if kind == "p":
        return Challenge(token, "pow", expires_at, pow=PowParams(prefix=token, difficulty_bits=bits))
    c = cfg.layers.captcha
    return Challenge(token, "captcha", expires_at, captcha=CaptchaParams(provider=c.provider, site_key=c.site_key))


async def _consume(rt: Runtime, cfg: DefencesConfig, s: Settings, tok: protocol.Token, now: int) -> bool:
    """Best-effort single use (Redis SETNX). Replay is harmless anyway (the challenge is bound to the
    user and event, and entry is idempotent), so this is defence in depth: Redis trouble => allow."""
    if not cfg.layers.pow.single_use:
        return True
    try:
        return bool(await rt.redis.set(f"fd:{s.env}:chal:used:{tok.mac}", "1", nx=True, ex=max(1, tok.exp - now + 5)))
    except (RedisError, OSError, TimeoutError) as exc:
        metrics.redis_error("single_use")
        log.warning("single-use tracking unavailable (%r); skipping", exc)
        return True


def _needs(cfg: DefencesConfig, risk: Risk | None) -> tuple[bool, bool]:
    """(pow required, captcha required) for this person right now."""
    L = cfg.layers
    risky = risk is not None and risk.challenge_required

    def need(layer) -> bool:  # pow and captcha layers share the same enabled/mode fields
        return layer.enabled and (layer.mode == "always" or (layer.mode == "risk" and risky))

    return need(L.pow), need(L.captcha)


async def _assess(rt: Runtime, s: Settings, cfg: DefencesConfig, ctx: GateContext, event_id: uuid.UUID,
                  user_id: uuid.UUID, now: datetime) -> Risk | None:
    L = cfg.layers
    if not (L.signals.enabled and L.risk.enabled):
        return None
    try:
        signals = await collect(rt, s.env, L.signals, event_id=str(event_id), user_id=user_id,
                                headers=ctx.request.headers, now=now)
        return assess(signals, L.signals, L.risk.thresholds)
    except Exception:  # noqa: BLE001
        # A signal outage must not stop people entering, but it must not be silent either: this entry
        # carries no risk breakdown and the log line is at ERROR.
        metrics.pg_error("risk_assessment")
        log.exception("risk assessment failed for user %s; proceeding without a score", user_id)
        return None


async def _decide(ctx: GateContext) -> tuple[GateDecision, Risk | None, uuid.UUID, uuid.UUID]:
    s = get_settings()
    event_id, user_id = _as_uuid(_field(ctx.event, "id")), _as_uuid(_field(ctx.user, "id"))
    cfg = await get_defences(event_id)
    rt = await get_runtime()
    risk = await _assess(rt, s, cfg, ctx, event_id, user_id, ctx.server_now)
    if risk is not None:
        metrics.RISK_BANDS.labels(risk.band).inc()
    pow_needed, cap_needed = _needs(cfg, risk)

    def finish(passed: list[str]) -> tuple[GateDecision, Risk | None, uuid.UUID, uuid.UUID]:
        info = risk.to_dict() if risk else {}
        if passed:
            info["challenges_passed"] = passed
        weight = risk.weight if risk else 1.0
        reason = f"allowed at weight {weight}" + (f" (risk {risk.score}, band {risk.band})" if risk else "")
        return GateDecision.allow(weight, reason, info or None), risk, event_id, user_id

    if not (pow_needed or cap_needed):
        return finish([])
    if not s.pow_secret:
        raise _unavailable("challenges are not configured: set JWT_SECRET or POW_SECRET", "challenge")

    now = int(ctx.server_now.timestamp())
    L = cfg.layers
    if cap_needed and await is_waived(rt, event_id, user_id):
        cap_needed = False  # accessible fallback granted by an organiser; PoW still applies
    load = await record_and_get(rt.redis, s.env, str(event_id), L.pow.load_ref_rps) if pow_needed else 0.0

    pow_done, cap_done = not pow_needed, not cap_needed
    cid = ctx.request.headers.get("x-challenge-id")
    solution = ctx.request.headers.get("x-challenge-solution", "")
    if cid:
        status, tok = protocol.check(cid, s.pow_secret, user_id, event_id, now)
        if status is protocol.Status.FORGED:
            metrics.CHALLENGES_FAILED.labels("any", "forged").inc()
            return GateDecision.reject("forged challenge token", layer="pow", risk=risk.to_dict() if risk else None), risk, event_id, user_id
        if status is not protocol.Status.OK or tok is None:
            kind = "pow" if not pow_done else "captcha"
            metrics.CHALLENGES_FAILED.labels(kind, status.value).inc()  # expired | wrong_binding
        elif tok.kind == "p":
            if not protocol.solution_ok(tok.raw, solution, tok.bits):
                metrics.CHALLENGES_FAILED.labels("pow", "bad_solution").inc()
            elif not await _consume(rt, cfg, s, tok, now):
                metrics.CHALLENGES_FAILED.labels("pow", "replay").inc()
            else:
                pow_done = True
                metrics.CHALLENGES_SOLVED.labels("pow").inc()
        elif cap_needed:
            if tok.pow_ok:
                pow_done = True  # signed by us when the PoW was verified
            if not await _consume(rt, cfg, s, tok, now):
                metrics.CHALLENGES_FAILED.labels("captcha", "replay").inc()
            else:
                ip = getattr(ctx.request.state, "client_ip", None)
                try:
                    cap_done = await _provider(cfg, s).verify(solution, user_id=user_id, event_id=event_id, remote_ip=ip)
                except ProviderUnavailable as exc:
                    metrics.CHALLENGES_FAILED.labels("captcha", "provider_error").inc()
                    log.error("captcha provider unavailable: %s", exc)
                    raise _unavailable("verification service is unavailable, try again shortly", "captcha") from None
                if cap_done:
                    metrics.CHALLENGES_SOLVED.labels("captcha").inc()
                else:
                    metrics.CHALLENGES_FAILED.labels("captcha", "bad_solution").inc()
        # EXPIRED / WRONG_BINDING / bad solution / replay: fall through to a fresh challenge.

    why = "challenge failed or expired" if cid else "challenge required"
    risk_info = risk.to_dict() if risk else None
    if not pow_done:
        bits = difficulty(L.pow, risk=risk.score if risk else 0.0, load=load)
        ch = _issue(cfg, s, "p", bits, False, event_id, user_id, now)
        return GateDecision.require_challenge(ch, why, layer="pow", risk=risk_info), risk, event_id, user_id
    if not cap_done:
        ch = _issue(cfg, s, "c", 0, True, event_id, user_id, now)
        return GateDecision.require_challenge(ch, why, layer="captcha", risk=risk_info), risk, event_id, user_id
    return finish([n for n, needed in (("pow", pow_needed), ("captcha", cap_needed)) if needed])


def _device(request: Any) -> str | None:
    raw = request.headers.get("x-device-id")
    try:
        return str(uuid.UUID(raw)) if raw else None
    except ValueError:
        return None


async def entry_gate(ctx: GateContext) -> GateDecision:
    decision, risk, event_id, user_id = await _decide(ctx)
    metrics.GATE_DECISIONS.labels(
        decision.action.value, decision.layer or "none", str(decision.weight) if decision.action is Action.ALLOW else "none"
    ).inc()
    try:  # logging must never fail or delay the request
        rt = await get_runtime()
        if rt.decisions is not None:
            rt.decisions.record({
                "event_id": event_id, "user_id": user_id, "ts": ctx.server_now, "action": decision.action.value,
                "weight": decision.weight if decision.action is Action.ALLOW else None,
                "score": risk.score if risk else None,
                "signals": decision.risk if decision.risk is not None else (risk.to_dict() if risk else None),
                "layer": decision.layer, "ip": getattr(ctx.request.state, "client_ip", None),
                "device": _device(ctx.request), "reason": decision.reason,
            })
    except Exception:  # noqa: BLE001
        log.exception("decision log record failed")
    return decision


async def required_challenge(event_id: uuid.UUID, user_id: uuid.UUID, now: datetime) -> Challenge | None:
    """What /defence/challenge pre-fetch returns: the first challenge this user must solve now, or None.
    Only layers in mode "always" are known without the request's headers; risk-mode challenges appear
    when the person actually enters."""
    s = get_settings()
    cfg = await get_defences(event_id)
    pow_needed, cap_needed = _needs(cfg, None)
    if not (pow_needed or cap_needed):
        return None
    if not s.pow_secret:
        raise _unavailable("challenges are not configured: set JWT_SECRET or POW_SECRET", "challenge")
    rt = await get_runtime()
    ts = int(now.timestamp())
    if pow_needed:
        load = await record_and_get(rt.redis, s.env, str(event_id), cfg.layers.pow.load_ref_rps)
        return _issue(cfg, s, "p", difficulty(cfg.layers.pow, 0.0, load), False, event_id, user_id, ts)
    if await is_waived(rt, event_id, user_id):
        return None
    return _issue(cfg, s, "c", 0, True, event_id, user_id, ts)
