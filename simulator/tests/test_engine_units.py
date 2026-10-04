"""Load-engine units: arrivals, population, recorder merge, and open-loop latency."""
from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest
from aiohttp import web

from fairdrop_sim.adapters.api_adapter import AuthConfig, Identity, Sender, identity_headers, make_session
from fairdrop_sim.crowd.arrivals import arrival_offsets
from fairdrop_sim.crowd.human import plan_humans
from fairdrop_sim.crowd.population import build_humans
from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.engine.shard import high_res_timer
from fairdrop_sim.models.scenario import ArrivalModel, LegitConfig, NatGroups

# --------------------------------------------------------------------------- arrivals


def test_spike_tail_shape():
    m = ArrivalModel(kind="spike_tail", spike_fraction=0.6, spike_window_fraction=0.05)
    a = arrival_offsets(100_000, 60.0, m, np.random.default_rng(1))
    assert a.min() >= 0 and a.max() < 60.0
    in_spike = np.mean(a < 3.0)  # 60% spike + 5% of the uniform 40%
    assert in_spike == pytest.approx(0.6 + 0.4 * 0.05, abs=0.01)
    assert np.mean(a < 0.9) > 0.6 * 0.6  # front-loaded inside the spike (tau = 0.9 s)


def test_uniform_shape_and_determinism():
    m = ArrivalModel(kind="uniform")
    a = arrival_offsets(50_000, 10.0, m, np.random.default_rng(7))
    b = arrival_offsets(50_000, 10.0, m, np.random.default_rng(7))
    assert np.array_equal(a, b)
    hist, _ = np.histogram(a, bins=10, range=(0, 10))
    assert hist.min() > 4700 and hist.max() < 5300


def test_population_fixed_across_runs_plans_vary():
    h1 = build_humans(1000, NatGroups(fraction=0.1, group_size=50), master_seed=5)
    h2 = build_humans(1000, NatGroups(fraction=0.1, group_size=50), master_seed=5)
    assert h1.user_ids == h2.user_ids and h1.client_ips == h2.client_ips
    assert len(set(h1.user_ids)) == 1000 and len(set(h1.device_ids)) == 1000
    nat_ips = [ip for ip, g in zip(h1.client_ips, h1.nat_group) if g >= 0]
    assert len(nat_ips) == 100 and len(set(nat_ips)) == 2  # 2 campus IPs x 50 users
    assert len(set(h1.client_ips)) == 900 + 2
    p0 = plan_humans(1000, 60, LegitConfig(), run_seed=1)
    p1 = plan_humans(1000, 60, LegitConfig(), run_seed=2)
    assert not np.array_equal(p0.arrival_s, p1.arrival_s)


def test_headers_carry_no_label():
    ident = Identity("u-1", "d-1", "10.0.0.1")
    h = identity_headers(ident, AuthConfig(mode="dev_header", sim_key="k"))
    assert h == {"X-Device-Id": "d-1", "X-User-Id": "u-1", "X-Sim-Key": "k", "X-Sim-Client-IP": "10.0.0.1"}
    assert not any("legit" in v or "bot" in v for v in h.values())
    with pytest.raises(ValueError):
        identity_headers(ident, AuthConfig(mode="jwt_sim_tokens"))


# --------------------------------------------------------------------------- recorder


def test_recorder_merge_equals_combined():
    a, b, both = Recorder(0.0), Recorder(0.0), Recorder(0.0)
    for i in range(100):
        r = a if i % 2 else b
        for target in (r, both):
            target.request("enter", "legit", 200, None, i * 0.01, i * 0.01, i * 0.01 + 0.005 * (i % 7 + 1))
    merged = Recorder.merged([a.to_dict(), b.to_dict()], 0.0)
    h, hb = merged.hist("enter"), both.hist("enter")
    assert h.get_total_count() == hb.get_total_count() == 100
    for q in (50, 95, 99):
        assert h.get_value_at_percentile(q) == hb.get_value_at_percentile(q)
    assert merged.count("enter", "legit", "ok") == 100


# --------------------------------------------------------------------------- open-loop latency


@pytest.fixture
def slow_server():
    """A server that takes 50 ms per request."""
    async def handler(_req):
        await asyncio.sleep(0.05)
        return web.json_response({"ok": True})

    async def start():
        app = web.Application()
        app.router.add_get("/x", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return runner, f"http://127.0.0.1:{port}"

    return start


def test_open_loop_latency_counts_queueing(slow_server):
    """Coordinated-omission check: 10 requests are due 10 ms apart but only one may be
    in flight and each takes 50 ms. A closed-loop tool would report ~50 ms for all.
    Open-loop latency (from the intended time) must grow with the backlog, while the
    service time stays at ~50 ms."""

    async def main():
        runner, url = await slow_server()
        rec = Recorder(0.0)
        try:
            async with make_session(url, 1) as s:
                sender = Sender(s, rec, max_in_flight=1, timeout_s=5)
                await sender.send("enter", "legit", time.perf_counter(), "GET", "/x", {})  # warm the connection
                rec.open_loop.clear()
                rec.service.clear()
                t0 = time.perf_counter() + 0.05
                await asyncio.gather(*(sender.send("enter", "legit", t0 + k * 0.01, "GET", "/x", {})
                                       for k in range(10)))
        finally:
            await runner.cleanup()
        return rec

    with high_res_timer():
        rec = asyncio.run(main())
    ol, sv = rec.hist("enter"), rec.hist("enter", kind="service")
    assert ol.get_total_count() == 10
    assert 45_000 <= sv.get_value_at_percentile(50) <= 90_000  # ~50 ms service time
    # last request waited for 9 others: ~ 10*50 - 9*10 = 410 ms from its intended time
    assert ol.get_max_value() >= 350_000
    assert ol.get_value_at_percentile(50) >= 2 * sv.get_value_at_percentile(50)
