"""Graceful drain: flag/event/exit hook, readiness, SSE streams told to reconnect, the signal handler."""
import asyncio
import importlib.util
import json
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn
from app.defence import lifecycle, metrics
from app.defence.metrics import REGISTRY

ROOT = Path(__file__).resolve().parents[2]
EV = "11111111-1111-1111-1111-111111111111"
ADM = {"X-Admin-Token": "adm"}


def val(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture(autouse=True)
def clean_lifecycle():
    lifecycle.reset_for_tests()
    yield
    lifecycle.reset_for_tests()


# ------------------------------------------------------------------------------ the state machine
async def test_begin_drain_sets_flag_event_and_metric_and_is_idempotent():
    assert not lifecycle.is_draining() and not lifecycle.drain_event().is_set()
    assert lifecycle.begin_drain(reason="test") is True
    assert lifecycle.is_draining() and lifecycle.drain_event().is_set() and val("fd_draining") == 1
    assert lifecycle.begin_drain() is False  # second call changes nothing


async def test_exit_callback_runs_after_the_requested_delay_only():
    fired = []
    lifecycle.register_exit(lambda: fired.append(time.monotonic()))
    t0 = time.monotonic()
    lifecycle.begin_drain(exit_after_s=0.3)
    await asyncio.sleep(0.1)
    assert fired == []  # still serving during the grace period
    await asyncio.sleep(0.4)
    assert len(fired) == 1 and fired[0] - t0 >= 0.28


async def test_a_drain_without_exit_never_stops_the_process():
    fired = []
    lifecycle.register_exit(lambda: fired.append(1))
    lifecycle.begin_drain()
    await asyncio.sleep(0.2)
    assert fired == []


# ------------------------------------------------------------------------------- readiness + admin
async def test_readyz_flips_to_503_when_draining(stub, client):
    assert (await client.get("/readyz")).status_code == 200
    lifecycle.begin_drain()
    r = await client.get("/readyz")
    assert r.status_code == 503 and "draining" in r.text and r.headers["retry-after"] == "1"
    assert (await client.get("/healthz")).status_code == 200  # liveness is unaffected: do not restart a draining replica


async def test_admin_drain_endpoint_needs_the_admin_token_and_reports_state(stub, client):
    assert (await client.post("/admin/defence/lifecycle/drain")).status_code == 401
    assert (await client.get("/admin/defence/lifecycle", headers=ADM)).json() == {"draining": False}
    r = await client.post("/admin/defence/lifecycle/drain", headers=ADM, json={"exit_after_s": 5})
    assert r.json() == {"draining": True, "started_now": True}
    assert (await client.post("/admin/defence/lifecycle/drain", headers=ADM)).json()["started_now"] is False
    assert (await client.get("/admin/defence/lifecycle", headers=ADM)).json() == {"draining": True}
    assert (await client.post("/admin/defence/lifecycle/drain", headers=ADM, json={"exit_after_s": -1})).status_code == 422


async def test_a_request_that_started_before_the_drain_still_completes(stub, client):
    slow = asyncio.create_task(client.get("/dev/slow?seconds=0.6"))
    await asyncio.sleep(0.15)
    lifecycle.begin_drain()
    r = await slow
    assert r.status_code == 200 and r.json() == {"slept": 0.6}  # not cut off


# ------------------------------------------------------------------------------------------- SSE
def parse(body: str) -> list[dict]:
    out = []
    for block in body.strip().split("\n\n"):
        ev = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                ev["comment"] = line[1:].strip()
            elif ":" in line:
                k, v = line.split(":", 1)
                ev[k] = v.strip()
        if ev:
            out.append(ev)
    return out


async def stream_until_drain(client, headers, run_for, during=None):
    task = asyncio.create_task(client.get(f"/events/{EV}/stream", headers=headers))
    await asyncio.sleep(0.3)
    if during:
        await during()
    await asyncio.sleep(run_for)
    lifecycle.begin_drain()
    return await asyncio.wait_for(task, 5)


async def test_stream_wire_format_then_reconnect_hint_on_drain(stub, client, monkeypatch):
    monkeypatch.setenv("SSE_POLL_S", "0.05")
    _, h = await stub.user()
    opened0, closed0 = val("fd_sse_streams_closed_for_drain_total"), val("fd_sse_streams_open")
    r = await stream_until_drain(client, h, 0.1)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    ev = parse(r.text)
    assert ev[0] == {"retry": "3000"}  # the retry hint comes first
    status = ev[1]
    assert status["id"] == "1" and status["event"] == "status"
    body = json.loads(status["data"])
    assert body["state"] == "REGISTERED" and body["phase"] in ("SCHEDULED", "OPEN", "CLOSED") and body["server_now"].endswith("Z")
    assert ev[-1]["event"] == "reconnect" and json.loads(ev[-1]["data"]) == {"reason": "server_draining"} and ev[-1]["retry"] == "1000"
    assert val("fd_sse_streams_closed_for_drain_total") == closed0 + 1
    assert val("fd_sse_streams_open") == 0  # the gauge returned to zero


async def test_stream_ids_continue_from_last_event_id(stub, client, monkeypatch):
    monkeypatch.setenv("SSE_POLL_S", "0.05")
    _, h = await stub.user()
    r = await stream_until_drain(client, {**h, "Last-Event-ID": "41"}, 0.05)
    assert [e["id"] for e in parse(r.text) if "id" in e][0] == "42"


async def test_stream_pushes_a_status_change_and_sends_heartbeats(stub, client, monkeypatch):
    monkeypatch.setenv("SSE_POLL_S", "0.05")
    monkeypatch.setenv("SSE_HEARTBEAT_S", "0.1")
    _, h = await stub.user()

    async def enter():
        assert (await client.post(f"/events/{EV}/enter", headers=h)).status_code == 201

    r = await stream_until_drain(client, h, 0.4, during=enter)
    ev = parse(r.text)
    states = [(e["id"], json.loads(e["data"])["state"]) for e in ev if e.get("event") == "status"]
    assert states == [("1", "REGISTERED"), ("2", "ENTERED")]  # a change is pushed with the next id
    assert any(e.get("comment") == "heartbeat" for e in ev)  # and silence is broken by heartbeats


async def test_stream_requires_identity_and_a_known_event(stub, client):
    assert (await client.get(f"/events/{EV}/stream")).status_code == 401
    _, h = await stub.user()
    assert (await client.get(f"/events/{uuid.uuid4()}/stream", headers=h)).status_code == 404


async def test_many_streams_all_get_the_hint_and_all_end(stub, client, monkeypatch):
    monkeypatch.setenv("SSE_POLL_S", "5")  # slow poll: they must be woken by the drain event, not by polling
    users = [await stub.user() for _ in range(8)]
    tasks = [asyncio.create_task(client.get(f"/events/{EV}/stream", headers=h)) for _, h in users]
    await asyncio.sleep(0.4)
    assert val("fd_sse_streams_open") == 8
    t0 = time.monotonic()
    lifecycle.begin_drain()
    results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
    assert time.monotonic() - t0 < 1.0  # woken immediately, not after the 5 s poll
    assert all(parse(r.text)[-1].get("event") == "reconnect" for r in results)
    assert val("fd_sse_streams_open") == 0


# ---------------------------------------------------------------------- the signal handler itself
def load_serve():
    spec = importlib.util.spec_from_file_location("fd_serve", ROOT / "infra" / "local" / "serve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_first_signal_drains_and_delays_the_stop_second_signal_stops_now(monkeypatch):
    serve = load_serve()
    started = []

    class FakeTimer:
        def __init__(self, interval, fn):
            started.append((interval, fn))

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", FakeTimer)
    server = serve.DrainingServer(uvicorn.Config("x:y"))
    server.drain_grace_s = 2.5

    server.handle_exit(15, None)  # first SIGTERM
    assert lifecycle.is_draining()
    assert server.should_exit is False  # NOT stopping yet: still serving while the gateway notices
    assert started and started[0][0] == 2.5

    started[0][1]()  # the grace period elapses
    assert server.should_exit is True

    server2 = serve.DrainingServer(uvicorn.Config("x:y"))
    server2.handle_exit(15, None)  # draining already: a second signal means "stop now"
    assert server2.should_exit is True


def test_without_the_defence_package_the_server_behaves_like_plain_uvicorn(monkeypatch):
    serve = load_serve()
    monkeypatch.setattr(serve, "_lifecycle", lambda: None)
    server = serve.DrainingServer(uvicorn.Config("x:y"))
    server.handle_exit(15, None)
    assert server.should_exit is True


async def test_draining_replicas_tag_every_response_so_the_gateway_can_react_at_once(stub, client):
    assert "x-draining" not in (await client.get("/healthz")).headers
    lifecycle.begin_drain()
    for path in ("/healthz", "/readyz", "/events"):
        assert (await client.get(path)).headers.get("x-draining") == "1", path
