import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.hooks import ALLOWED_WEIGHTS, GateAction, GateDecision
from app.services.beacon import DrandBeacon, MockBeacon


@pytest.mark.parametrize("cls", [MockBeacon, DrandBeacon])
def test_round_after_is_first_round_strictly_after(cls):
    b = cls()
    t = datetime(2026, 10, 3, 12, 0, 0, 500_000, tzinfo=UTC)
    r = b.round_after(t)
    assert b.round_time(r) > t
    assert b.round_time(r - 1) <= t


def test_round_after_on_exact_boundary():
    b = MockBeacon()
    t = b.round_time(5000)
    assert b.round_after(t) == 5001          # strictly after
    assert b.round_after(t - timedelta(microseconds=1)) == 5000


def test_quicknet_schedule_matches_drand_info():
    # From GET https://api.drand.sh/<quicknet>/info (checked 2026-10-03).
    assert DrandBeacon.genesis == 1692803367 and DrandBeacon.period == 3
    assert DrandBeacon().round_time(1) == datetime.fromtimestamp(1692803367, UTC)


async def test_mock_beacon_deterministic():
    v = await MockBeacon().fetch(42)
    assert v.randomness == hashlib.sha256(b"fairdrop-mock-beacon:42").hexdigest()
    assert (await MockBeacon().fetch(42)) == v


def test_gate_decision_weight_validation():
    for w in ALLOWED_WEIGHTS:
        GateDecision(weight=w)
    for bad in (0.3, 2.0, -1.0, 0.75):
        with pytest.raises(ValueError):
            GateDecision(weight=bad)
    assert GateDecision().action == GateAction.ALLOW and GateDecision().weight == 1.0
