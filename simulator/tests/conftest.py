from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mock_server import ManualClock, Settings, create_app

ADMIN = {"X-Admin-Token": "test-admin"}
SEED_A = "aa" * 32
SEED_B = "bb" * 32


class Mock:
    """Test driver around the mock app with a manual clock."""

    def __init__(self, client: TestClient, clock: ManualClock):
        self.c = client
        self.clock = clock

    def create(self, **kw) -> str:
        body = {"inventory": 3, "window_seconds": 60, "claim_ttl_seconds": 30, "server_seed_hex": SEED_A, **kw}
        r = self.c.post("/admin/events", headers=ADMIN, json=body)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def admin(self, method: str, path: str, **kw):
        return self.c.request(method, path, headers=ADMIN, **kw)

    def enter(self, ev: str, uid: str, **headers):
        return self.c.post(f"/events/{ev}/enter", headers={"X-User-Id": uid, **headers})

    def status(self, ev: str, uid: str):
        return self.c.get(f"/events/{ev}/status", headers={"X-User-Id": uid})

    def claim(self, ev: str, uid: str, key: str | None = None):
        return self.c.post(f"/events/{ev}/claim",
                           headers={"X-User-Id": uid, "Idempotency-Key": key or f"key-{uid}-0001"})

    def run_lottery(self, ev: str, users: list[str], seed: str | None = None) -> dict[str, dict]:
        self.admin("POST", f"/admin/events/{ev}/open")
        for u in users:
            assert self.enter(ev, u).status_code == 200
        assert self.admin("POST", f"/admin/events/{ev}/close").status_code == 200
        r = self.admin("POST", f"/admin/events/{ev}/draw", json={"server_seed_hex": seed} if seed else {})
        assert r.status_code == 200, r.text
        return {u: self.status(ev, u).json() for u in users}

    def invariants(self, ev: str) -> dict:
        return self.admin("GET", f"/admin/events/{ev}/invariants").json()


@pytest.fixture
def mock():
    clock = ManualClock()
    app = create_app(Settings(auth_mode="dev", simulation_mode=True, sim_key="test-sim", admin_token="test-admin",
                              tick_interval_s=3600), clock)
    with TestClient(app) as client:
        yield Mock(client, clock)
