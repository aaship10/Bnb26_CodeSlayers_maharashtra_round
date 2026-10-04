import pytest
from app.defence.config import DefenceConfigError, effective_config, validate_defences
from app.defence.config_schema import DefencesConfig, RiskThresholds
from app.defence.presets import PRESET_NAMES, list_presets, preset_config

ENABLED = {
    "none": set(),
    "rate_limit": {"rate_limit"},
    "rate_limit+pow": {"rate_limit", "pow"},
    "rate_limit+pow+captcha": {"rate_limit", "pow", "captcha"},
    "all": {"rate_limit", "pow", "captcha", "signals", "risk"},
    "custom": set(),
}


def enabled_layers(cfg: DefencesConfig) -> set[str]:
    return {n for n in ("rate_limit", "pow", "captcha", "signals", "risk") if getattr(cfg.layers, n).enabled}


def test_preset_names_match_contract():
    assert set(PRESET_NAMES) == {"none", "rate_limit", "rate_limit+pow", "rate_limit+pow+captcha", "all", "custom"}


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_every_preset_is_valid_and_enables_the_documented_layers(name):
    cfg = validate_defences({"preset": name})
    assert cfg.preset == name
    assert enabled_layers(cfg) == ENABLED[name]


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_preset_round_trips_through_json(name):
    dumped = preset_config(name).model_dump(mode="json")
    assert validate_defences(dumped) == preset_config(name)


def test_presets_endpoint_payload_lists_full_layers():
    payload = list_presets()
    assert [p["id"] for p in payload] == list(PRESET_NAMES)
    for p in payload:
        assert set(p) == {"id", "name", "description", "defences"}
        assert set(p["defences"]["layers"]) == {"rate_limit", "pow", "captcha", "signals", "risk"}
        assert p["defences"]["preset"] == p["id"]


def test_preset_config_returns_independent_copies():
    a = preset_config("all")
    a.layers.pow.base_bits = 30
    assert preset_config("all").layers.pow.base_bits == 14


def test_missing_config_means_no_defences():
    assert enabled_layers(effective_config(None)) == set()
    assert enabled_layers(effective_config({})) == set()
    assert enabled_layers(effective_config({"defences": None})) == set()


def test_layers_without_preset_is_custom():
    cfg = validate_defences({"layers": {"rate_limit": {"enabled": True}}})
    assert cfg.preset == "custom"
    assert enabled_layers(cfg) == {"rate_limit"}


def test_named_preset_rejects_overriding_layers():
    with pytest.raises(DefenceConfigError) as exc:
        validate_defences({"preset": "rate_limit", "layers": {"rate_limit": {"enabled": True, "retry_jitter_max": 0.1}}})
    assert exc.value.errors[0]["type"] == "preset_mismatch"


def test_named_preset_accepts_identical_layers():
    blob = preset_config("all").model_dump(mode="json")
    assert validate_defences(blob).preset == "all"


def test_fail_mode_is_orthogonal_to_presets():
    assert validate_defences({"preset": "rate_limit", "fail_mode": "closed"}).fail_mode == "closed"
    assert validate_defences({"preset": "none"}).fail_mode == "local"


@pytest.mark.parametrize(
    "blob",
    [
        {"preset": "bogus"},
        {"preset": "custom", "layers": {"rate_limit": {"enabeld": True}}},  # typo must not pass silently
        {"preset": "custom", "unknown_top_level": 1},
        {"preset": "custom", "fail_mode": "maybe"},
        "not-an-object",
        ["list"],
    ],
)
def test_malformed_blobs_are_rejected(blob):
    with pytest.raises(DefenceConfigError):
        validate_defences(blob)


@pytest.mark.parametrize(
    "layers",
    [
        {"risk": {"enabled": True}},  # risk without signals
        {"pow": {"enabled": True, "mode": "risk"}},  # risk-mode pow without risk engine
        {"captcha": {"enabled": True, "mode": "risk"}},
        {"pow": {"enabled": True, "base_bits": 20, "max_bits": 10}},
        {"captcha": {"enabled": True, "provider": "turnstile", "site_key": ""}},
        {"signals": {"enabled": True, "weights": {"account_age": 1.5}}},
        {"signals": {"enabled": True, "weights": {"sybil_sense": 0.5}}},
        {"rate_limit": {"enabled": True, "retry_jitter_max": 0.5}},  # spec: jitter is 0..25%
        {"rate_limit": {"enabled": True, "limits": {"enter": {"ip": {"capacity": 0, "refill_per_s": 1}}}}},
        {"rate_limit": {"enabled": True, "limits": {"enter": {"galaxy": {"capacity": 1, "refill_per_s": 1}}}}},
    ],
)
def test_invalid_layer_combinations_are_rejected(layers):
    with pytest.raises(DefenceConfigError):
        validate_defences({"preset": "custom", "layers": layers})


def test_valid_risk_mode_combination_accepted():
    cfg = validate_defences(
        {
            "preset": "custom",
            "layers": {
                "signals": {"enabled": True},
                "risk": {"enabled": True},
                "pow": {"enabled": True, "mode": "risk"},
            },
        }
    )
    assert enabled_layers(cfg) == {"signals", "risk", "pow"}


def test_thresholds_must_be_strictly_ordered():
    RiskThresholds(challenge=0.1, weight_half=0.2, weight_quarter=0.3)
    for t in ({"challenge": 0.5, "weight_half": 0.5, "weight_quarter": 0.9}, {"challenge": 0.9, "weight_half": 0.5, "weight_quarter": 0.6}):
        with pytest.raises(ValueError):
            RiskThresholds(**t)


def test_no_reject_threshold_exists():
    # REJECT is reserved for hard evidence; a score threshold for it must not be configurable.
    assert "reject" not in RiskThresholds.model_fields


def test_status_limit_matches_one_request_per_two_seconds():
    b = preset_config("rate_limit").layers.rate_limit.limits["status"]["identity"]
    assert b.refill_per_s == 0.5


def test_ip_limits_are_looser_than_identity_limits():
    # Guardrail: a campus NAT shares one IP, so per-IP must allow far more than per-identity.
    for endpoint, dims in preset_config("rate_limit").layers.rate_limit.limits.items():
        assert dims["ip"].refill_per_s >= 20 * dims["identity"].refill_per_s, endpoint
        assert dims["subnet"].capacity >= dims["ip"].capacity, endpoint
