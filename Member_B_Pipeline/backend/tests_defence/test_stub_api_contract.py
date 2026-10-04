"""Contract checks that need no database: routing, error shape, admin auth.
(DB-backed flows live in test_identity_flow.py, against the local Postgres.)"""
import pytest
from fastapi.testclient import TestClient

ADMIN = {"X-Admin-Token": "t0ken"}


@pytest.fixture
def client(settings_env):
    settings_env(ADMIN_TOKEN="t0ken")
    from app.main import app

    # No `with`: skip lifespan, so no Postgres is needed.
    return TestClient(app, raise_server_exceptions=False)


def test_healthz_is_db_free(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_expected_routes_exist(client):
    # OpenAPI rather than app.routes: included routers are wrapped in newer FastAPI versions.
    paths = set(client.get("/openapi.json").json()["paths"])
    for p in (
        "/healthz",
        "/readyz",
        "/events/{event_id}",
        "/events/{event_id}/enter",
        "/events/{event_id}/status",
        "/admin/events/{event_id}/config",
        "/admin/events/{event_id}/invariants",
        "/admin/defence/presets",
        "/auth/register",
        "/auth/verify",
        "/auth/refresh",
        "/auth/me",
        "/admin/sim/tokens",
    ):
        assert p in paths, p
    # Backend routes carry no /api prefix; nginx strips it.
    assert not any(p.startswith("/api") for p in paths)


def test_missing_identity_is_401_with_contract_body(client):
    # /auth/me (no rate-limit dependency, so no database needed for this unit test)
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHENTICATED" and "message" in r.json()


def test_bad_uuid_is_validation_error_shape(client):
    r = client.get("/events/not-a-uuid")
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "VALIDATION_ERROR" and "errors" in body["details"]


def test_unknown_route_uses_error_contract(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


def test_presets_endpoint_requires_admin_token(client):
    assert client.get("/admin/defence/presets").status_code == 401
    assert client.get("/admin/defence/presets", headers={"X-Admin-Token": "wrong"}).status_code == 403
    r = client.get("/admin/defence/presets", headers=ADMIN)
    assert r.status_code == 200
    assert isinstance(r.json(), list) and r.json()[0]["id"] == "none"


def test_admin_fails_closed_when_token_unset(settings_env):
    settings_env()  # no ADMIN_TOKEN
    from app.main import app

    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/admin/defence/presets", headers={"X-Admin-Token": ""}).status_code == 403
    assert c.get("/admin/defence/presets", headers={"X-Admin-Token": "anything"}).status_code == 403


def test_config_patch_requires_admin_token(client):
    r = client.patch("/admin/events/11111111-1111-1111-1111-111111111111/config", json={"defences": {"preset": "x"}})
    assert r.status_code == 401  # no admin token
