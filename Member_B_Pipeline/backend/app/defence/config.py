"""Validation and normalisation of events.config.defences.

A's PATCH /admin/events/{id}/config treats the blob as opaque; whoever stores it
should run it through validate_defences() first (see docs/INTERFACE_REQUESTS_B.md).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from .config_schema import DefencesConfig
from .errors import clean_validation_errors
from .presets import preset_config


class DefenceConfigError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("invalid defences config")
        self.errors = errors


def validate_defences(blob: Any) -> DefencesConfig:
    """Parse and normalise a defences blob.

    * Named preset, no `layers`: layers are expanded from the preset.
    * Named preset WITH `layers`: allowed only if identical to the preset. A silent
      override would make experiment labels lie; use preset="custom" to deviate.
    * `layers` without `preset`: treated as "custom".
    * `fail_mode` is orthogonal and may be set with any preset.
    """
    if blob is None:
        blob = {"preset": "none"}
    if not isinstance(blob, Mapping):
        raise DefenceConfigError([{"loc": [], "msg": "defences must be an object", "type": "type_error"}])

    raw = dict(blob)
    raw.setdefault("preset", "custom" if "layers" in raw else "none")
    try:
        cfg = DefencesConfig.model_validate(raw)
    except ValidationError as exc:
        raise DefenceConfigError(clean_validation_errors(exc.errors())) from exc

    if cfg.preset != "custom":
        expected = preset_config(cfg.preset)
        if "layers" in raw:
            if cfg.layers != expected.layers:
                raise DefenceConfigError(
                    [
                        {
                            "loc": ["layers"],
                            "msg": f"layers differ from preset {cfg.preset!r}; use preset='custom' to override",
                            "type": "preset_mismatch",
                        }
                    ]
                )
        else:
            cfg = cfg.model_copy(update={"layers": expected.layers})
    return cfg


def effective_config(event_config: Mapping[str, Any] | None) -> DefencesConfig:
    """What the gate should enforce for an event. Missing or null → no defences."""
    return validate_defences((event_config or {}).get("defences"))
