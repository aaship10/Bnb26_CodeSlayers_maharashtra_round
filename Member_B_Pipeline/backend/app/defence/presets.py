"""The named defence presets Member C sweeps over in experiments."""
from __future__ import annotations

from typing import Any

from .config_schema import (
    CaptchaLayer,
    DefencesConfig,
    Layers,
    PowLayer,
    RateLimitLayer,
    RiskLayer,
    SignalsLayer,
)


def _build() -> dict[str, tuple[str, DefencesConfig]]:
    rl = RateLimitLayer(enabled=True)
    pow_always = PowLayer(enabled=True, mode="always")
    captcha_always = CaptchaLayer(enabled=True, mode="always", provider="mock")
    captcha_risk = CaptchaLayer(enabled=True, mode="risk", provider="mock")

    def cfg(name: str, **layers: Any) -> DefencesConfig:
        return DefencesConfig(preset=name, layers=Layers(**layers))  # type: ignore[arg-type]

    return {
        "none": ("Baseline: no defences. Allocation rule only.", cfg("none")),
        "rate_limit": ("Edge + Redis rate limiting only.", cfg("rate_limit", rate_limit=rl)),
        "rate_limit+pow": (
            "Rate limiting plus a proof-of-work challenge for every entrant.",
            cfg("rate_limit+pow", rate_limit=rl, pow=pow_always),
        ),
        "rate_limit+pow+captcha": (
            "Rate limiting, PoW and CAPTCHA for every entrant (maximum friction, no risk engine).",
            cfg("rate_limit+pow+captcha", rate_limit=rl, pow=pow_always, captcha=captcha_always),
        ),
        "all": (
            "Everything: rate limits, PoW for all, CAPTCHA and down-weighting driven by the risk engine.",
            cfg(
                "all",
                rate_limit=rl,
                pow=pow_always,
                captcha=captcha_risk,
                signals=SignalsLayer(enabled=True),
                risk=RiskLayer(enabled=True),
            ),
        ),
        # Template for hand-tuned configs; everything off so nothing is implied.
        "custom": ("Template for hand-tuned configs. Layers are taken from the request as given.", cfg("custom")),
    }


_PRESETS = _build()
PRESET_NAMES: tuple[str, ...] = tuple(_PRESETS)


def preset_config(name: str) -> DefencesConfig:
    """A fresh deep copy, so callers can never mutate the registry."""
    return _PRESETS[name][1].model_copy(deep=True)


def list_presets() -> list[dict[str, Any]]:
    return [
        # `defences` is exactly what PATCH /admin/events/{id}/config accepts under the same key.
        {"id": name, "name": name, "description": desc, "defences": cfg.model_dump(mode="json")}
        for name, (desc, cfg) in _PRESETS.items()
    ]
