import ipaddress

import httpx
import pytest
from app.defence.clientip import ClientIPMiddleware, parse_ip, resolve_client, subnet_of
from app.defence.simheaders import sim_authorized
from fastapi import FastAPI, Request

TRUST = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("127.0.0.1/32"))
PROXY = "10.1.1.1"


def resolve(peer, headers=None, trust=TRUST, sim=False):
    return resolve_client(peer, {k.lower(): v for k, v in (headers or {}).items()}, trust, sim)


def test_untrusted_peer_cannot_spoof_forwarding_headers():
    r = resolve("198.51.100.9", {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"})
    assert (r.ip, r.source) == ("198.51.100.9", "peer")


def test_trusted_proxy_forwarded_for_is_used():
    r = resolve(PROXY, {"X-Forwarded-For": "203.0.113.7"})
    assert (r.ip, r.source) == ("203.0.113.7", "forwarded")


def test_leftmost_entry_is_not_believed():
    # client sent "X-Forwarded-For: 1.1.1.1"; proxy appended the real peer it saw.
    r = resolve(PROXY, {"X-Forwarded-For": "1.1.1.1, 203.0.113.7"})
    assert r.ip == "203.0.113.7"


def test_trusted_hops_are_skipped_from_the_right():
    r = resolve(PROXY, {"X-Forwarded-For": "203.0.113.7, 10.2.2.2, 10.3.3.3"})
    assert r.ip == "203.0.113.7"


def test_real_ip_fallback_and_garbage_handling():
    assert resolve(PROXY, {"X-Real-IP": "203.0.113.8"}).ip == "203.0.113.8"
    assert resolve(PROXY, {"X-Forwarded-For": "garbage, ???", "X-Real-IP": "203.0.113.8"}).ip == "203.0.113.8"
    assert resolve(PROXY, {"X-Forwarded-For": "garbage"}).ip == PROXY  # nothing usable: fall back to peer
    assert resolve(PROXY, {}).source == "peer"


def test_no_trusted_proxies_means_headers_never_count():
    assert resolve(PROXY, {"X-Forwarded-For": "203.0.113.7"}, trust=()).ip == PROXY


def test_ipv4_mapped_ipv6_is_normalised_and_ports_are_tolerated():
    assert str(parse_ip("::ffff:203.0.113.7")) == "203.0.113.7"
    assert str(parse_ip("203.0.113.7:5555")) == "203.0.113.7"
    assert str(parse_ip("[2001:db8::1]:443")) == "2001:db8::1"
    assert parse_ip("999.1.1.1") is None and parse_ip("") is None and parse_ip(None) is None


def test_subnets():
    assert subnet_of(parse_ip("203.0.113.77")) == "203.0.113.0/24"
    assert subnet_of(parse_ip("2001:db8:1:2:3:4:5:6")) == "2001:db8:1:2::/64"


def test_unknown_peer_maps_to_a_single_bucket_not_an_exception():
    assert resolve(None).ip == "0.0.0.0"


def test_sim_client_ip_only_honoured_when_authorised():
    h = {"X-Sim-Client-IP": "198.51.100.77"}
    assert resolve("198.51.100.9", h, sim=False).ip == "198.51.100.9"
    r = resolve("198.51.100.9", h, sim=True)
    assert (r.ip, r.source) == ("198.51.100.77", "sim")
    assert resolve("198.51.100.9", {"X-Sim-Client-IP": "nonsense"}, sim=True).ip == "198.51.100.9"


# ---- simulation-header authorisation (SIMULATION-only headers are rejected when off)
def test_sim_headers_are_rejected_when_simulation_mode_is_off(settings_env):
    settings_env(SIM_KEY="secret")  # key configured, mode off
    assert sim_authorized({"x-sim-key": "secret", "x-sim-client-ip": "1.2.3.4"}) is False


def test_sim_headers_need_the_right_key_when_on(settings_env):
    settings_env(SIMULATION_MODE="true", SIM_KEY="secret")
    assert sim_authorized({"x-sim-key": "secret"}) is True
    assert sim_authorized({"x-sim-key": "wrong"}) is False
    assert sim_authorized({}) is False
    assert sim_authorized({"x-sim-key": ""}) is False


# ---- end to end through the ASGI middleware
async def _whoami(settings_env, peer, headers, **env):
    settings_env(TRUSTED_PROXIES="127.0.0.1/32", **env)
    app = FastAPI()
    app.add_middleware(ClientIPMiddleware)

    @app.get("/ip")
    async def ip(request: Request):
        return {"ip": request.state.client_ip, "subnet": request.state.client_subnet}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)), base_url="http://t") as c:
        return (await c.get("/ip", headers=headers)).json()


async def test_middleware_publishes_client_ip(settings_env):
    out = await _whoami(settings_env, "127.0.0.1", {"X-Forwarded-For": "203.0.113.50"})
    assert out == {"ip": "203.0.113.50", "subnet": "203.0.113.0/24"}


async def test_middleware_ignores_spoofing_from_untrusted_peer(settings_env):
    out = await _whoami(settings_env, "198.51.100.9", {"X-Forwarded-For": "203.0.113.50", "X-Sim-Client-IP": "203.0.113.51"})
    assert out["ip"] == "198.51.100.9"


async def test_middleware_honours_sim_ip_only_with_key(settings_env):
    env = dict(SIMULATION_MODE="true", SIM_KEY="k")
    good = await _whoami(settings_env, "198.51.100.9", {"X-Sim-Client-IP": "203.0.113.51", "X-Sim-Key": "k"}, **env)
    bad = await _whoami(settings_env, "198.51.100.9", {"X-Sim-Client-IP": "203.0.113.51", "X-Sim-Key": "nope"}, **env)
    assert good["ip"] == "203.0.113.51" and bad["ip"] == "198.51.100.9"
