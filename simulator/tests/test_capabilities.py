"""Capability detection: what the target implements, and the showable sentence when it is incomplete."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fairdrop_sim.adapters.capabilities import REQUIRED_FOR_FULL_RUN, Capabilities, describe_gap, from_openapi
from fairdrop_sim.adapters.driver import DriverUnavailable, TargetMismatch, make_driver
from fairdrop_sim.doctor import FAIL, OK, WARN, run_doctor
from tests.test_engine_e2e import mock_url  # noqa: F401  (module-scoped in-process mock)

A_STAGE2 = json.loads((Path(__file__).parent / "fixtures" / "a_stage2_openapi.json").read_text(encoding="utf-8"))


def test_member_a_stage2_has_the_entry_path_and_nothing_after_the_window():
    """Against A's REAL OpenAPI document (origin/main 10277c8). If this fails, A's API moved: update
    the fixture and these expectations on purpose."""
    routes = from_openapi(A_STAGE2)
    for present in ("schedule", "open", "close", "enter", "status"):
        assert routes[present], present
    for absent in ("draw", "claim", "reset", "stats", "invariants", "stream", "readyz",
                   "defence_presets", "defence_decisions", "sim_tokens"):
        assert not routes[absent], absent


def test_incomplete_engine_is_described_in_one_showable_sentence():
    caps = Capabilities(kind="real", reachable=True, base_url="http://x:8000", routes=from_openapi(A_STAGE2))
    assert caps.missing() == ["draw", "claim"] and not caps.complete and not caps.draw
    msg = describe_gap(caps)
    assert "reachable but incomplete" in msg and "draw, claim" in msg and "Member A" in msg


def test_unreachable_target_is_reported_not_raised():
    caps = Capabilities(base_url="http://x:9")
    assert "not reachable" in describe_gap(caps)
    assert set(caps.missing()) == set(REQUIRED_FOR_FULL_RUN)


def test_route_matching_needs_the_right_method_and_event_scope():
    doc = {"paths": {"/admin/events/{event_id}/draw": {"get": {}},          # wrong method
                     "/draw": {"post": {}},                                  # not event scoped
                     "/events/{event_id}/claim": {"post": {}}}}
    r = from_openapi(doc)
    assert r["claim"] is True and r["draw"] is False


@pytest.mark.asyncio
async def test_mock_is_complete_and_doctor_says_ready(mock_url):  # noqa: F811
    rep = await run_doctor(mock_url, "dev-admin-token")
    assert rep.ok_for_full_run and rep.caps.kind == "mock" and rep.caps.complete
    assert not [c for c in rep.checks if c.level == FAIL]
    assert any(c.name == "route draw" and c.level == OK for c in rep.checks)


@pytest.mark.asyncio
async def test_doctor_flags_a_bad_admin_token(mock_url):  # noqa: F811
    rep = await run_doctor(mock_url, "wrong-token")
    bad = [c for c in rep.checks if c.name == "admin token"]
    assert bad and bad[0].level == FAIL and not rep.ok_for_full_run


@pytest.mark.asyncio
async def test_doctor_on_nothing_fails_fast():
    rep = await run_doctor("http://127.0.0.1:9", "x")
    assert not rep.ok_for_full_run and rep.checks[0].level == FAIL and "nothing answers" in rep.checks[0].detail


@pytest.mark.asyncio
async def test_make_driver_refuses_a_target_of_the_wrong_kind(mock_url):  # noqa: F811
    with pytest.raises(TargetMismatch, match="'mock'"):
        await make_driver(mock_url, "dev-admin-token", expected_target="real")
    d = await make_driver(mock_url, "dev-admin-token", expected_target="mock")
    assert type(d).__name__ == "MockDriver" and not d.manual_window
    await d.admin.aclose()
    with pytest.raises(DriverUnavailable, match="nothing is answering"):
        await make_driver("http://127.0.0.1:9", "x", expected_target="mock")
