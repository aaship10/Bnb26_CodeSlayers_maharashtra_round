"""Pure tests: signal functions, the risk engine (monotonicity, IP cap, weights), ASN lookup."""
import random

import pytest
from app.defence.config_schema import RiskThresholds, SignalsLayer
from app.defence.contracts import ALLOWED_WEIGHTS
from app.defence.risk.engine import assess, band_and_weight, combine
from app.defence.signals import features as f
from app.defence.signals.asn import asn_of
from app.defence.signals.features import IP_ONLY_SIGNALS, Signal

CFG = SignalsLayer(enabled=True)
TH = RiskThresholds()
ALL_NAMES = list(CFG.weights)


def sigs(**values):
    return [Signal(n, values.get(n, 0.0)) for n in ALL_NAMES]


# -------------------------------------------------------------------------------- features
def test_signal_values_are_bounded():
    with pytest.raises(ValueError):
        Signal("x", 1.01)
    with pytest.raises(ValueError):
        Signal("x", -0.01)


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"user-agent": "Mozilla/5.0 Chrome/126", "accept-language": "en-IN"}, 0.0),
        ({"accept-language": "en"}, 1.0),  # no UA at all
        ({"user-agent": "python-requests/2.31", "accept-language": "en"}, 0.9),
        ({"user-agent": "curl/8.0"}, 0.9),
        ({"user-agent": "Go-http-client/1.1"}, 0.9),
        ({"user-agent": "Mozilla/5.0 HeadlessChrome/126", "accept-language": "en"}, 0.6),
        ({"user-agent": "Mozilla/5.0 Firefox/127"}, 0.4),  # browser UA but no Accept-Language
    ],
)
def test_header_anomaly(headers, expected):
    assert f.header_anomaly(headers).value == expected


def test_timing_needs_enough_samples_and_separates_machines_from_people():
    assert f.timing_regularity([1000.0] * 3, min_samples=5).value == 0.0  # not enough evidence
    assert f.timing_regularity([1000.0] * 8, 5).value == 1.0  # a sleep(1) loop
    assert f.timing_regularity([1000, 1010, 990, 1005, 995, 1000], 5).value > 0.9
    human = [2300, 800, 5100, 1200, 9000, 400, 3100]
    assert f.timing_regularity(human, 5).value == 0.0
    backoff = [500, 1000, 2000, 4000, 8000, 16000]  # exponential retry backoff: what an honest client does
    assert f.timing_regularity(backoff, 5).value == 0.0
    # A loop that sleeps 1 s +/- 25% (a "polite" bot) is still only HALF suspicious: by design the signal is
    # weak evidence, capped by its weight, and never decides anything alone.
    jittered = [1000 * (1 + random.Random(i).uniform(-0.25, 0.25)) for i in range(12)]
    assert 0.3 < f.timing_regularity(jittered, 5).value < 0.7


def test_timing_degenerate_inputs():
    assert f.timing_regularity([0.0] * 6, 5).value == 1.0  # a burst with zero gaps
    assert f.timing_regularity([], 5).value == 0.0


@pytest.mark.parametrize("n,expected", [(1, 0.0), (2, 0.1), (3, 0.2), (11, 1.0), (500, 1.0)])
def test_accounts_per_device(n, expected):
    assert f.accounts_per_device(n).value == pytest.approx(expected)


def test_network_counts_are_logarithmic():
    assert f.accounts_per_ip(1).value == 0.0
    assert f.accounts_per_ip(10).value == pytest.approx(1 / 3, abs=0.01)
    assert f.accounts_per_ip(1000).value == 1.0 and f.accounts_per_ip(10**6).value == 1.0
    assert f.accounts_per_subnet(2000).value == 1.0 and f.accounts_per_asn(20000).value == 1.0


def test_account_age_and_velocity_and_otp():
    assert f.account_age(0).value == 1.0 and f.account_age(21600).value == 0.0 and f.account_age(-50).value == 1.0
    assert f.account_age(10800).value == pytest.approx(0.5)
    assert f.registration_velocity(20).value == 0.0 and f.registration_velocity(200).value == 1.0
    assert f.registration_velocity(110).value == pytest.approx(0.5)
    assert f.otp_latency(None).value == 0.0 and f.otp_latency(500).value == 1.0
    assert f.otp_latency(8000).value == 0.0 and f.otp_latency(5000).value == pytest.approx(0.5)


def test_email_flags():
    assert f.email_pattern({"sequential_pattern": True, "pattern_cluster_size": 9}).value == 1.0
    assert f.email_pattern({}).value == 0.0
    assert f.email_entropy({"random_local_part": True}).value == 1.0 and f.email_entropy({}).value == 0.0


# ---------------------------------------------------------------------------------- asn
def test_asn_lookup_uses_the_dummy_table_and_longest_prefix():
    assert asn_of("10.20.5.5") == (64512, "10.20.0.0/16", "CAMPUS-NAT")
    assert asn_of("203.0.113.9")[0] == 64522
    assert asn_of("172.20.1.1")[0] == 64530
    assert asn_of("8.8.8.8") is None and asn_of("not-an-ip") is None and asn_of(None) is None
    assert asn_of("2001:db8::1") is None  # no IPv6 rows in the dummy table


# ------------------------------------------------------------------------------ risk engine
def test_no_signals_means_zero_risk_and_full_weight():
    r = assess(sigs(), CFG, TH)
    assert (r.score, r.weight, r.band, r.challenge_required) == (0.0, 1.0, "low", False)


def test_score_is_the_noisy_or_of_weight_times_value():
    r = assess(sigs(accounts_per_device=1.0, email_pattern=1.0), CFG, TH)
    expected = 1 - (1 - 0.45) * (1 - 0.30)
    assert r.score == pytest.approx(expected, abs=1e-5)
    assert {p.name: p.contribution for p in r.parts if p.contribution}["accounts_per_device"] == pytest.approx(0.45)


def test_every_contribution_is_recorded_and_explainable():
    r = assess(sigs(accounts_per_device=0.5, otp_latency=1.0), CFG, TH)
    d = r.to_dict()
    names = {s["name"] for s in d["signals"]}
    assert names == {"accounts_per_device", "otp_latency"}  # zero contributions omitted
    survive = 1.0
    for s in d["signals"]:
        assert s["contribution"] == pytest.approx(s["weight"] * s["value"], abs=1e-3)
        survive *= 1 - s["contribution"]
    assert d["score"] == pytest.approx(1 - survive, abs=2e-3)  # a reader can recompute the score


@pytest.mark.parametrize(
    "score,band,weight",
    [(0.0, "low", 1.0), (0.299, "low", 1.0), (0.30, "challenge", 1.0), (0.549, "challenge", 1.0),
     (0.55, "half", 0.5), (0.749, "half", 0.5), (0.75, "quarter", 0.25), (1.0, "quarter", 0.25)],
)
def test_band_boundaries(score, band, weight):
    assert band_and_weight(score, TH) == (band, weight)


def test_only_allowed_weights_ever_come_out():
    rng = random.Random(3)
    seen = set()
    for _ in range(3000):
        r = assess([Signal(n, rng.random() if rng.random() < 0.5 else 0.0) for n in ALL_NAMES], CFG, TH)
        assert r.weight in ALLOWED_WEIGHTS and 0.0 <= r.score <= 1.0
        seen.add(r.weight)
    assert seen == {1.0, 0.5, 0.25}  # the sample actually exercised every band


def test_monotonicity_adding_or_raising_a_bad_signal_never_lowers_the_score():
    rng = random.Random(11)
    for _ in range(1500):
        base = {n: (rng.random() if rng.random() < 0.6 else 0.0) for n in ALL_NAMES}
        before = assess(sigs(**base), CFG, TH).score
        name = rng.choice(ALL_NAMES)
        raised = dict(base)
        raised[name] = min(1.0, base[name] + rng.random())
        after = assess(sigs(**raised), CFG, TH)
        assert after.score >= before - 1e-9, (name, base, raised)
        assert assess(sigs(**raised), CFG, TH).weight <= assess(sigs(**base), CFG, TH).weight  # lower or equal weight


def test_ip_only_signals_are_capped_and_cannot_trigger_a_challenge_alone():
    # a campus NAT: every network signal maxed out
    r = assess(sigs(accounts_per_ip=1.0, accounts_per_subnet=1.0, accounts_per_asn=1.0, registration_velocity=1.0), CFG, TH)
    assert r.score <= CFG.ip_only_cap + 1e-9
    assert CFG.ip_only_cap < TH.challenge  # the invariant that makes "never penalise a shared IP alone" true
    assert (r.band, r.weight, r.challenge_required) == ("low", 1.0, False)


def test_the_cap_does_not_swallow_non_network_evidence():
    net = assess(sigs(accounts_per_ip=1.0, accounts_per_subnet=1.0, accounts_per_asn=1.0), CFG, TH).score
    both = assess(sigs(accounts_per_ip=1.0, accounts_per_subnet=1.0, accounts_per_asn=1.0, accounts_per_device=1.0), CFG, TH)
    assert both.score > net + 0.3  # device evidence still moves the score by its full weight
    assert both.challenge_required


def test_cap_is_configurable_and_ip_names_are_exactly_the_network_signals():
    assert IP_ONLY_SIGNALS == {"accounts_per_ip", "accounts_per_subnet", "accounts_per_asn", "registration_velocity"}
    loose = SignalsLayer(enabled=True, ip_only_cap=1.0)
    assert assess(sigs(accounts_per_ip=1.0, accounts_per_subnet=1.0, accounts_per_asn=1.0), loose, TH).score > 0.25
    zero = SignalsLayer(enabled=True, ip_only_cap=0.0)
    assert assess(sigs(accounts_per_ip=1.0), zero, TH).score == 0.0


def test_zero_weight_signals_are_ignored_and_unknown_names_are_harmless():
    cfg = SignalsLayer(enabled=True, weights={"accounts_per_device": 0.0})
    assert assess([Signal("accounts_per_device", 1.0), Signal("mystery", 1.0)], cfg, TH).score == 0.0


def test_combine_returns_one_part_per_signal():
    score, parts = combine(sigs(otp_latency=1.0), CFG)
    assert len(parts) == len(ALL_NAMES) and score == pytest.approx(0.15)


def test_a_typical_bot_farm_account_lands_in_a_down_weight_band():
    # 200 accounts on one device, registered with scripted speed, scripted headers
    r = assess(sigs(accounts_per_device=1.0, otp_latency=1.0, header_anomaly=0.9, account_age=0.9), CFG, TH)
    assert r.band in ("half", "quarter") and r.weight < 1.0
