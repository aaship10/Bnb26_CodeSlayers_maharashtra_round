"""The no-Docker runtime: env generation and the dev gateway's behaviour."""
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / "infra" / "local"


def load(name: str):
    sys.path.insert(0, str(LOCAL))
    spec = importlib.util.spec_from_file_location(name, LOCAL / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ env file
def test_env_example_has_every_variable_the_code_needs():
    text = (ROOT / "infra" / ".env.example").read_text(encoding="utf-8")
    keys = {ln.split("=", 1)[0] for ln in text.splitlines() if "=" in ln and not ln.startswith("#")}
    for k in ("ADMIN_TOKEN", "AUTH_MODE", "JWT_SECRET", "OTP_PEPPER", "SIMULATION_MODE", "SIM_KEY", "TRUSTED_PROXIES",
              "POSTGRES_PASSWORD", "POSTGRES_PORT", "HTTP_PORT", "API_BASE_PORT", "ALLOWED_EMAIL_DOMAINS", "SMTP_HOST"):
        assert k in keys, k


def test_env_example_contains_no_real_secret_values():
    for ln in (ROOT / "infra" / ".env.example").read_text(encoding="utf-8").splitlines():
        if ln.startswith(("ADMIN_TOKEN=", "JWT_SECRET=", "OTP_PEPPER=", "SIM_KEY=", "POSTGRES_PASSWORD=")):
            assert ln.split("=", 1)[1].strip() == "__GENERATE__", ln


def test_ensure_env_generates_distinct_secrets_and_never_overwrites(tmp_path, monkeypatch):
    common = load("common")
    monkeypatch.setattr(common, "ENV_FILE", tmp_path / ".env")
    common.ensure_env()
    vals = dict(ln.split("=", 1) for ln in (tmp_path / ".env").read_text().splitlines() if "=" in ln and not ln.startswith("#"))
    secrets_ = [vals[k] for k in ("ADMIN_TOKEN", "JWT_SECRET", "OTP_PEPPER", "SIM_KEY", "POSTGRES_PASSWORD")]
    assert all(s and s != "__GENERATE__" and len(s) >= 32 for s in secrets_)
    assert len(set(secrets_)) == len(secrets_)
    assert vals["SIMULATION_MODE"] == "false"  # simulation is opt-in
    (tmp_path / ".env").write_text("ADMIN_TOKEN=keepme\n")
    common.ensure_env()
    assert (tmp_path / ".env").read_text() == "ADMIN_TOKEN=keepme\n"


def test_gitignore_excludes_secrets_and_runtime_state():
    gi = (ROOT / ".gitignore").read_text()
    assert "infra/.env" in gi and ".local/" in gi


# ------------------------------------------------------------------- gateway
@pytest.fixture
def gw(tmp_path, monkeypatch):
    mod = load("gateway")
    rep = tmp_path / "replicas"
    rep.write_text("127.0.0.1:8001\n127.0.0.1:8002\n")
    monkeypatch.setattr(mod, "REPLICAS_FILE", rep)
    monkeypatch.setattr(mod, "_cache", (-1.0, []))
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>spa</h1>")
    (web / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(mod, "FRONTEND", web.resolve())
    mod.seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        mod.seen.append(request)
        if request.url.port == 8001 and getattr(mod, "dead_8001", False):
            raise httpx.ConnectError("refused", request=request)
        body = json.dumps({"port": request.url.port, "path": request.url.path, "query": request.url.query.decode()}).encode()
        # a streamed body, like a real upstream: MockTransport's json= shortcut arrives pre-read,
        # which the gateway's aiter_raw() streaming would (rightly) refuse
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(mod, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    return mod


async def call(mod, method="GET", path="/", peer="198.51.100.9", **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mod.app, client=(peer, 4000)), base_url="http://gw") as c:
        return await c.request(method, path, **kw)


async def test_gateway_strips_api_prefix_and_keeps_query(gw):
    r = await call(gw, path="/api/events/abc?x=1&y=2")
    assert r.status_code == 200 and r.json()["path"] == "/events/abc" and r.json()["query"] == "x=1&y=2"


async def test_gateway_overwrites_client_forwarding_headers(gw):
    await call(gw, path="/api/x", headers={"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8", "Host": "evil"})
    h = gw.seen[-1].headers
    assert h["x-forwarded-for"] == "198.51.100.9" and h["x-real-ip"] == "198.51.100.9"


async def test_gateway_round_robins_and_fails_over_on_refused_connection(gw):
    ports = {(await call(gw, path="/api/x")).json()["port"] for _ in range(6)}
    assert ports == {8001, 8002}
    gw.dead_8001 = True
    for method in ("GET", "POST"):  # safe for POST: nothing reached the dead replica
        for _ in range(4):
            r = await call(gw, method, "/api/x")
            assert r.status_code == 200 and r.json()["port"] == 8002


async def test_gateway_503_when_no_replica_is_available(gw, tmp_path):
    gw.REPLICAS_FILE.write_text("")
    gw._cache = (-1.0, [])
    r = await call(gw, path="/api/x")
    assert r.status_code == 503 and r.headers["retry-after"] == "1" and r.json()["code"] == "INTERNAL"


async def test_gateway_picks_up_replica_list_changes_without_restart(gw):
    import os
    import time

    gw.REPLICAS_FILE.write_text("127.0.0.1:8002\n")
    os.utime(gw.REPLICAS_FILE, (time.time() + 5, time.time() + 5))  # force an mtime change
    assert {(await call(gw, path="/api/x")).json()["port"] for _ in range(4)} == {8002}


async def test_gateway_does_not_expose_metrics(gw):
    r = await call(gw, path="/api/metrics")
    assert r.status_code == 404 and gw.seen == []


async def test_gateway_serves_frontend_with_spa_fallback_and_blocks_traversal(gw):
    assert (await call(gw, path="/app.js")).text.startswith("console")
    assert "spa" in (await call(gw, path="/some/client/route")).text
    r = await call(gw, path="/%2e%2e/%2e%2e/etc/passwd")
    assert "root:" not in r.text
    r = await call(gw, path="/..%5c..%5csecret")
    assert r.status_code in (200, 404) and "secret" not in r.text.lower()


async def test_gateway_oversized_body_is_rejected(gw):
    r = await call(gw, "POST", "/api/x", content=b"x" * 1_000_001)
    assert r.status_code == 413


# --------------------------------------------------------------- gateway: edge behaviour
@pytest.fixture
def edge(gw, monkeypatch):
    """The gw fixture's mock upstream, plus a counter and switchable failures for edge tests."""
    gw.calls = []
    state = {"mode": "ok", "now": "2000-01-01T00:00:00.000Z"}
    gw.state = state

    def upstream(request: httpx.Request) -> httpx.Response:
        gw.calls.append((request.method, request.url.path, dict(request.headers)))
        if state["mode"] == "fail_after_send":  # simulates a connection reset after the request was sent
            raise httpx.ReadError("reset", request=request)
        body = json.dumps({"server_now": state["now"], "path": request.url.path}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(gw, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    monkeypatch.setattr(gw, "_mc", {})
    monkeypatch.setattr(gw, "_mc_locks", {})
    monkeypatch.setattr(gw, "_buckets", {})
    return gw


async def test_microcache_collapses_a_stampede_into_one_upstream_request(edge):
    rs = await asyncio.gather(*[call(edge, path="/api/events/abc") for _ in range(30)])
    assert all(r.status_code == 200 for r in rs)
    assert len([c for c in edge.calls if c[1] == "/events/abc"]) == 1
    assert sorted({r.headers["x-cache"] for r in rs}) == ["HIT", "MISS"]


async def test_microcache_hit_gets_a_fresh_server_now(edge):
    first = (await call(edge, path="/api/events/abc")).json()
    assert first["server_now"] == "2000-01-01T00:00:00.000Z"  # the miss passes the upstream value through
    hit = await call(edge, path="/api/events/abc")
    assert hit.headers["x-cache"] == "HIT"
    assert hit.json()["server_now"] != "2000-01-01T00:00:00.000Z" and hit.json()["server_now"].endswith("Z")
    # a list endpoint is patched per item
    lst = await call(edge, path="/api/events")
    assert lst.status_code == 200


async def test_microcache_expires_and_only_covers_public_gets(edge, monkeypatch):
    monkeypatch.setattr(edge, "MICROCACHE_TTL_S", 0.2)
    await call(edge, path="/api/events/abc")
    await asyncio.sleep(0.3)
    await call(edge, path="/api/events/abc")
    assert len([c for c in edge.calls if c[1] == "/events/abc"]) == 2  # refreshed after the TTL
    for path in ("/api/events/abc/status", "/api/auth/me"):
        await call(edge, path=path)
        await call(edge, path=path)
    assert len([c for c in edge.calls if c[1] == "/events/abc/status"]) == 2  # per-user data is never cached
    await call(edge, "POST", "/api/events/abc/enter")
    assert (await call(edge, "POST", "/api/events/abc")).headers.get("x-cache") is None  # POST never cached


async def test_edge_limiter_is_per_ip_and_returns_the_429_contract(edge, monkeypatch):
    monkeypatch.setattr(edge, "EDGE_BURST", 5.0)
    monkeypatch.setattr(edge, "EDGE_RPS", 0.001)
    codes = [(await call(edge, path="/api/x")).status_code for _ in range(8)]
    assert codes == [200] * 5 + [429] * 3
    r = await call(edge, path="/api/x")
    assert r.json()["code"] == "RATE_LIMITED" and r.json()["details"]["scope"] == "ip" and int(r.headers["retry-after"]) >= 1
    assert (await call(edge, path="/api/x", peer="198.51.100.77")).status_code == 200  # another client is unaffected
    # the static frontend is not behind the API limiter
    assert (await call(edge, path="/")).status_code == 200


async def test_edge_limiter_bypassed_only_with_a_valid_simulation_key(edge, monkeypatch):
    monkeypatch.setattr(edge, "EDGE_BURST", 1.0)
    monkeypatch.setattr(edge, "EDGE_RPS", 0.001)
    monkeypatch.setattr(edge, "SIM_MODE", True)
    monkeypatch.setattr(edge, "SIM_KEY", "k")
    assert [(await call(edge, path="/api/x", headers={"X-Sim-Key": "k"})).status_code for _ in range(5)] == [200] * 5
    await call(edge, path="/api/x")
    assert (await call(edge, path="/api/x", headers={"X-Sim-Key": "wrong"})).status_code == 429
    monkeypatch.setattr(edge, "SIM_MODE", False)
    assert (await call(edge, path="/api/x", headers={"X-Sim-Key": "k"})).status_code == 429  # mode off: key means nothing


async def test_edge_limiter_can_be_disabled(edge, monkeypatch):
    monkeypatch.setattr(edge, "EDGE_RPS", 0)
    assert {(await call(edge, path="/api/x")).status_code for _ in range(50)} == {200}


async def test_replay_after_send_only_for_enter_and_claim(edge):
    edge.state["mode"] = "fail_after_send"
    # /enter is idempotent by contract: tried on both replicas, then a clean 502
    r = await call(edge, "POST", "/api/events/abc/enter")
    assert r.status_code == 502 and len(edge.calls) == 2
    edge.calls.clear()
    # /claim only with an Idempotency-Key
    await call(edge, "POST", "/api/events/abc/claim")
    assert len(edge.calls) == 1
    edge.calls.clear()
    await call(edge, "POST", "/api/events/abc/claim", headers={"Idempotency-Key": "k1"})
    assert len(edge.calls) == 2
    edge.calls.clear()
    # anything else is NEVER replayed: a second /auth/register could send a second email
    await call(edge, "POST", "/api/auth/register", json={})
    assert len(edge.calls) == 1


async def test_sse_gets_no_buffering_headers_and_a_long_read_timeout(edge, monkeypatch):
    seen = {}
    orig = edge._client.build_request

    def spy(method, url, **kw):
        seen["timeout"] = kw.get("timeout")
        return orig(method, url, **kw)

    monkeypatch.setattr(edge._client, "build_request", spy)
    r = await call(edge, path="/api/events/abc/stream")
    assert r.headers["x-accel-buffering"] == "no" and r.headers["cache-control"] == "no-cache"
    assert seen["timeout"].read >= 3600  # heartbeats are ~20 s apart; a 15 s read timeout would kill the stream


# ------------------------------------------------------------- gateway: health-aware routing (drain)
R1, R2 = "http://127.0.0.1:8001", "http://127.0.0.1:8002"


@pytest.fixture
def hgw(gw, monkeypatch):
    monkeypatch.setattr(gw, "_ready", {})
    monkeypatch.setattr(gw, "_down_until", {})
    monkeypatch.setattr(gw, "EDGE_RPS", 0)  # these tests are about routing, not the edge limiter
    return gw


async def ports(mod, n=8, path="/api/x"):
    return [(await call(mod, path=path)).json()["port"] for _ in range(n)]


async def test_a_replica_that_reports_not_ready_gets_no_new_requests(hgw):
    assert set(await ports(hgw)) == {8001, 8002}
    hgw._ready[R1] = False  # what the health loop sets when /readyz answers 503 (draining)
    assert set(await ports(hgw, 12)) == {8002}
    hgw._ready[R1] = True
    assert set(await ports(hgw)) == {8001, 8002}  # and it rejoins when ready again


async def test_if_every_replica_looks_unready_the_gateway_still_tries_them(hgw):
    hgw._ready[R1] = hgw._ready[R2] = False
    r = await call(hgw, path="/api/x")
    assert r.status_code == 200  # a stale health view must never turn into a total outage


async def test_a_refused_connection_takes_the_replica_out_of_rotation_immediately(hgw):
    hgw.dead_8001 = True
    await ports(hgw, 4)  # the first attempts discover the failure
    hgw.seen.clear()
    await ports(hgw, 10)
    assert not [r for r in hgw.seen if r.url.port == 8001]  # no more attempts at it during the penalty window


async def test_passive_penalty_expires(hgw, monkeypatch):
    hgw._down_until[R1] = time.monotonic() - 1  # already expired
    assert set(await ports(hgw)) == {8001, 8002}


async def test_probe_reads_readiness(hgw, monkeypatch):
    def handler(request):
        if request.url.port == 8001:
            return httpx.Response(503, text="draining")
        if request.url.port == 8002:
            return httpx.Response(200, json={"status": "ready"})
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(hgw, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await hgw._probe(R1) is False and await hgw._probe(R2) is True and await hgw._probe("http://127.0.0.1:9") is False


async def test_health_loop_marks_the_draining_replica_down_within_one_interval(hgw, monkeypatch):
    import asyncio

    monkeypatch.setattr(hgw, "HEALTH_INTERVAL_S", 0.05)
    def handler(r):
        if r.url.path == "/readyz":
            return httpx.Response(503) if r.url.port == 8001 else httpx.Response(200)
        body = json.dumps({"port": r.url.port}).encode()  # proxied traffic: a streamed body, like a real upstream
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(hgw, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    task = asyncio.create_task(hgw.health_loop())
    await asyncio.sleep(0.2)
    task.cancel()
    assert hgw._ready == {R1: False, R2: True}
    assert set(await ports(hgw)) == {8002}


# ----------------------------------------------------------------------------- gateway: ops metrics
PROM_A = """# HELP fd_http_requests_total x
# TYPE fd_http_requests_total counter
fd_http_requests_total{method="GET",route="/a",status="200"} 5.0
fd_http_requests_created{method="GET",route="/a",status="200"} 1.7e9
# TYPE fd_redis_breaker_open gauge
fd_redis_breaker_open 0.0
"""
PROM_B = PROM_A.replace("} 5.0", "} 7.0")


@pytest.fixture
def ops(hgw, monkeypatch):
    monkeypatch.setattr(hgw, "ADMIN_TOKEN", "tok")

    def handler(request):
        if request.url.path == "/metrics":
            return httpx.Response(200, text=PROM_A if request.url.port == 8001 else PROM_B)
        if request.url.path == "/readyz":
            return httpx.Response(200, json={}) if request.url.port == 8001 else httpx.Response(503, text='{"message":"draining"}')
        return httpx.Response(404)

    monkeypatch.setattr(hgw, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return hgw


async def test_ops_metrics_requires_the_admin_token(ops):
    assert (await call(ops, path="/ops/api/metrics")).status_code == 403
    assert (await call(ops, path="/ops/api/metrics", headers={"X-Admin-Token": "wrong"})).status_code == 403
    ops.ADMIN_TOKEN = ""
    assert (await call(ops, path="/ops/api/metrics", headers={"X-Admin-Token": ""})).status_code == 403  # unset token = closed


async def test_ops_metrics_sums_the_replicas_and_reports_their_state(ops):
    r = await call(ops, path="/ops/api/metrics", headers={"X-Admin-Token": "tok"})
    body = r.json()
    total = [v for n, lbl, v in body["samples"] if n == "fd_http_requests_total" and lbl["status"] == "200"]
    assert total == [12.0]  # 5 + 7: counters add across replicas
    assert not any(n.endswith("_created") for n, _, _ in body["samples"])
    by = {x["base"]: x for x in body["replicas"]}
    assert by[R1] == {"base": R1, "up": True, "ready": True, "draining": False}
    assert by[R2]["up"] is True and by[R2]["ready"] is False and by[R2]["draining"] is True


async def test_ops_page_is_served_and_not_cached(ops):
    r = await call(ops, path="/ops/")
    assert r.status_code == 200 and "Fair Drop ops" in r.text and r.headers["cache-control"] == "no-store"
    assert (await call(ops, path="/api/metrics")).status_code == 404  # still not exposed on the public API path


async def test_gateway_stops_routing_to_a_replica_the_moment_it_says_it_is_draining(hgw, monkeypatch):
    def handler(request):
        body = json.dumps({"port": request.url.port}).encode()
        headers = {"content-type": "application/json"}
        if request.url.port == 8001:
            headers["x-draining"] = "1"
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    monkeypatch.setattr(hgw, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    seen = [(await call(hgw, path="/api/x")).json()["port"] for _ in range(2)]  # one of these may hit 8001 once
    assert hgw._ready.get(R1) is False  # marked down by the header alone, no readiness poll needed
    assert set([(await call(hgw, path="/api/x")).json()["port"] for _ in range(10)]) == {8002}
    assert 8002 in seen or 8001 in seen


# ------------------------------------------------------------------ nginx edge templates
NGINX_EXE = ROOT / ".local" / "nginx" / "nginx.exe"


@pytest.mark.skipif(not NGINX_EXE.exists(), reason="nginx not downloaded (python infra/local/get_nginx.py)")
@pytest.mark.parametrize("ports,sim_key", [([8001], None), ([8001, 8002, 8003], "k" * 48)])
def test_rendered_nginx_config_is_valid(tmp_path, ports, sim_key):
    """Renders the templates into a scratch prefix (never touches the running stack's config) and asks nginx to check them."""
    run = load("run")
    run.nginx_render(ports, 18080, 18090, sim_key, {}, base=tmp_path)
    ok, out = run.nginx_test(base=tmp_path)
    assert ok, out
    text = (tmp_path / "conf" / "fd" / "nginx.conf").read_text(encoding="utf-8")
    for f in (tmp_path / "conf" / "fd").glob("*.conf"):
        left = [k for k in ("__EDGE_", "__UPSTREAM_", "__LISTEN__", "__SIM_KEY_MAP__", "__OPS_PORT__", "__FRONTEND_ROOT__") if k in f.read_text(encoding="utf-8")]
        assert not left, (f.name, left)  # no placeholder left unfilled
    assert ("limit_req_zone" in text) and (("k" * 48 in text) == bool(sim_key))  # the key is only rendered in simulation mode
    assert text.count("server 127.0.0.1:80") == len(ports)
