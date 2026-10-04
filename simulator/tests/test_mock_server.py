"""Contract behaviour of the mock target (status codes follow D's INTERFACE_REQUESTS_D A5)."""
from __future__ import annotations

import hashlib

from fairdrop_sim.challenge.pow import solve
from tests.conftest import SEED_A, SEED_B

USERS = [f"u{i:03d}" for i in range(20)]


def winners(statuses: dict[str, dict]) -> set[str]:
    return {u for u, s in statuses.items() if s["state"] == "WON"}


def test_window_errors_and_codes(mock):
    ev = mock.create()
    r = mock.enter(ev, "u1")
    assert (r.status_code, r.json()["code"]) == (409, "WINDOW_NOT_OPEN")
    mock.admin("POST", f"/admin/events/{ev}/open")
    assert mock.enter(ev, "u1").status_code == 200
    mock.clock.advance(61)
    r = mock.enter(ev, "u2")
    assert (r.status_code, r.json()["code"]) == (409, "WINDOW_CLOSED")
    assert mock.c.get(f"/events/{ev}").json()["phase"] == "DRAWING"


def test_enter_is_idempotent(mock):
    ev = mock.create()
    mock.admin("POST", f"/admin/events/{ev}/open")
    a = mock.enter(ev, "u1").json()
    mock.clock.advance(5)
    b = mock.enter(ev, "u1").json()
    assert a["already_entered"] is False and b["already_entered"] is True
    assert a["entered_at"] == b["entered_at"]
    assert mock.admin("GET", f"/admin/events/{ev}/stats").json()["entries"] == 1
    mock.clock.advance(100)  # a replay after the window closes is answered, not an error
    assert mock.enter(ev, "u1").json()["already_entered"] is True


def test_no_rank_fields_before_draw(mock):
    ev = mock.create()
    mock.admin("POST", f"/admin/events/{ev}/open")
    mock.enter(ev, "u1")
    s = mock.status(ev, "u1").json()
    assert s["state"] == "ENTERED" and set(s) == {"state", "phase", "server_now"}
    assert mock.status(ev, "nobody").json()["state"] == "REGISTERED"


def test_draw_deterministic_order_independent_and_seed_sensitive(mock):
    ev = mock.create(id="evt_same")
    w1 = winners(mock.run_lottery(ev, USERS))
    mock.admin("POST", f"/admin/events/{ev}/reset", json={"server_seed_hex": SEED_A})
    w2 = winners(mock.run_lottery(ev, list(reversed(USERS))))  # different arrival order
    assert w1 == w2 and len(w1) == 3
    mock.admin("POST", f"/admin/events/{ev}/reset", json={"server_seed_hex": SEED_B})
    assert winners(mock.run_lottery(ev, USERS)) != w1


def test_draw_verifies_and_commitment_matches(mock):
    ev = mock.create()
    before = mock.c.get(f"/events/{ev}").json()
    assert before["server_seed_hex"] is None
    mock.run_lottery(ev, USERS)
    after = mock.c.get(f"/events/{ev}").json()
    assert hashlib.sha256(bytes.fromhex(after["server_seed_hex"])).hexdigest() == before["seed_commitment"]
    assert mock.admin("GET", f"/__mock/events/{ev}/verify").json()["verified"] is True


def test_claim_flow_and_idempotency(mock):
    ev = mock.create()
    st = mock.run_lottery(ev, USERS)
    w = sorted(winners(st))
    loser = next(u for u in USERS if st[u]["state"] == "WAITLISTED")
    assert st[loser]["waitlist_position"] >= 1
    r1 = mock.claim(ev, w[0], "same-key-123")
    r2 = mock.claim(ev, w[0], "same-key-123")
    assert r1.status_code == r2.status_code == 200
    pick = lambda r: (r.json()["seat_no"], r.json()["ticket_code"])  # noqa: E731
    assert pick(r1) == pick(r2)
    r3 = mock.claim(ev, w[0], "other-key-456")
    assert (r3.status_code, r3.json()["code"]) == (409, "ALREADY_CLAIMED")
    r4 = mock.claim(ev, loser)
    assert (r4.status_code, r4.json()["code"]) == (403, "NOT_WINNER")
    r5 = mock.c.post(f"/events/{ev}/claim", headers={"X-User-Id": w[1]})
    assert (r5.status_code, r5.json()["code"]) == (400, "VALIDATION_ERROR")
    s = mock.status(ev, w[0]).json()
    assert s["state"] == "CLAIMED" and s["seat_no"] == r1.json()["seat_no"]
    assert mock.invariants(ev)["passed"] is True


def test_expiry_promotes_waitlist_in_strict_order(mock):
    ev = mock.create()
    st = mock.run_lottery(ev, USERS)
    w = sorted(winners(st))
    queue = sorted((s["waitlist_position"], u) for u, s in st.items() if s["state"] == "WAITLISTED")
    mock.claim(ev, w[0])
    mock.clock.advance(31)  # the two unclaimed holds expire
    r = mock.claim(ev, w[1])
    assert (r.status_code, r.json()["code"]) == (410, "HOLD_EXPIRED")
    promoted = {u for u in USERS if mock.status(ev, u).json()["state"] == "WON"}
    assert promoted == {queue[0][1], queue[1][1]}
    assert mock.status(ev, queue[2][1]).json()["waitlist_position"] == 1
    assert mock.invariants(ev)["passed"] is True


def test_event_closes_and_waitlist_becomes_lost(mock):
    ev = mock.create(inventory=2)
    st = mock.run_lottery(ev, USERS[:4])
    for u in winners(st):
        mock.claim(ev, u)
    mock.clock.advance(1)
    assert mock.c.get(f"/events/{ev}").json()["phase"] == "CLOSED"
    states = sorted(mock.status(ev, u).json()["state"] for u in USERS[:4])
    assert states == ["CLAIMED", "CLAIMED", "LOST", "LOST"]


def test_fcfs_first_n_arrivals_win(mock):
    ev = mock.create(mode="FCFS")
    mock.admin("POST", f"/admin/events/{ev}/open")
    states = [mock.enter(ev, u).json()["state"] for u in USERS[:6]]
    assert states == ["WON"] * 3 + ["WAITLISTED"] * 3
    assert mock.admin("POST", f"/admin/events/{ev}/draw").status_code == 409
    assert mock.invariants(ev)["passed"] is True


def test_rate_limit_429_shape(mock):
    ev = mock.create(config={"defences": {"preset": "rate_limit", "layers": {"rate_limit": {"per_user_burst": 2}}}})
    mock.admin("POST", f"/admin/events/{ev}/open")
    codes = [mock.status(ev, "u1").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = mock.status(ev, "u1")
    body = r.json()
    assert body["code"] == "RATE_LIMITED" and body["details"]["scope"] == "user"
    assert body["details"]["retry_after_ms"] > 0 and int(r.headers["Retry-After"]) >= 1
    mock.clock.advance(1)
    assert mock.status(ev, "u1").status_code == 200


def test_pow_challenge_flow(mock):
    ev = mock.create(config={"defences": {"preset": "custom",
                                          "layers": {"pow": {"enabled": True, "difficulty_bits": 8}}}})
    mock.admin("POST", f"/admin/events/{ev}/open")
    r = mock.enter(ev, "u1")
    assert (r.status_code, r.json()["code"]) == (403, "CHALLENGE_REQUIRED")
    ch = r.json()["details"]["challenge"]
    assert ch["type"] == "pow" and ch["pow"]["algo"] == "sha256-lzb"
    bad = mock.enter(ev, "u1", **{"X-Challenge-Id": ch["id"], "X-Challenge-Solution": "x"})
    assert bad.json()["details"]["reason"] == "invalid_solution"
    ch = bad.json()["details"]["challenge"]
    sol = solve(ch["pow"]["prefix"], ch["pow"]["difficulty_bits"])
    ok = mock.enter(ev, "u1", **{"X-Challenge-Id": ch["id"], "X-Challenge-Solution": sol.nonce})
    assert ok.status_code == 200 and ok.json()["state"] == "ENTERED"
    # a solution is single-use and bound to its user
    other = mock.enter(ev, "u2", **{"X-Challenge-Id": ch["id"], "X-Challenge-Solution": sol.nonce})
    assert other.json()["code"] == "CHALLENGE_REQUIRED"


def test_captcha_mock_token(mock):
    ev = mock.create(config={"defences": {"preset": "custom", "layers": {"captcha": {"enabled": True}}}})
    mock.admin("POST", f"/admin/events/{ev}/open")
    ch = mock.enter(ev, "u1").json()["details"]["challenge"]
    assert ch["captcha"] == {"provider": "mock", "site_key": "mock-site-key"}
    ok = mock.enter(ev, "u1", **{"X-Challenge-Id": ch["id"], "X-Challenge-Solution": "mock-captcha-ok"})
    assert ok.status_code == 200


def test_sim_headers_risk_and_decisions(mock):
    ev = mock.create(config={"defences": {"preset": "all", "layers": {
        "pow": {"enabled": False}, "captcha": {"enabled": False}, "rate_limit": {"enabled": False},
        "risk": {"ip_users_half": 3, "ip_users_quarter": 6}}}})
    mock.admin("POST", f"/admin/events/{ev}/open")
    bad = mock.enter(ev, "u1", **{"X-Sim-Client-IP": "10.0.0.1", "X-Sim-Key": "wrong"})
    assert (bad.status_code, bad.json()["code"]) == (403, "FORBIDDEN")
    for i in range(8):  # 8 identities behind one fake IP
        r = mock.enter(ev, f"bot{i}", **{"X-Sim-Client-IP": "10.0.0.1", "X-Sim-Key": "test-sim",
                                         "X-Device-Id": f"d{i}"})
        assert r.status_code == 200
    mock.enter(ev, "human", **{"X-Device-Id": "dh"})
    mock.admin("POST", f"/admin/events/{ev}/close")
    mock.admin("POST", f"/admin/events/{ev}/draw")
    rows = {e["user_id"]: e for e in mock.admin("GET", f"/__mock/events/{ev}/export").json()["entries"]}
    assert rows["bot0"]["client_ip"] == "10.0.0.1" and rows["bot0"]["weight"] == 0.25
    assert rows["human"]["weight"] == 1.0
    assert "sim_label" not in rows["bot0"]
    decisions = mock.admin("GET", "/admin/defence/decisions", params={"event_id": ev}).json()
    assert {d["user_id"] for d in decisions if d["action"] == "downweight"} == {f"bot{i}" for i in range(8)}
    assert mock.admin("GET", f"/__mock/events/{ev}/verify").json()["verified"] is True


def test_auth_and_admin(mock):
    ev = mock.create()
    r = mock.c.post(f"/events/{ev}/enter")
    assert (r.status_code, r.json()["code"]) == (401, "UNAUTHENTICATED")
    r = mock.c.post("/admin/events", headers={"X-Admin-Token": "nope"}, json={})
    assert (r.status_code, r.json()["code"]) == (403, "FORBIDDEN")
    toks = mock.admin("POST", "/admin/sim/tokens", json={"user_ids": ["jw1"]}).json()["tokens"]
    mock.admin("POST", f"/admin/events/{ev}/open")
    assert mock.c.post(f"/events/{ev}/enter", headers={"Authorization": f"Bearer {toks['jw1']}"}).status_code == 200
    assert mock.c.post(f"/events/{ev}/enter", headers={"Authorization": "Bearer mjwt.jw1.forged"}).status_code == 401
    assert mock.c.get("/events/nope").json()["code"] == "NOT_FOUND"


def test_presets_and_config(mock):
    presets = {p["id"]: p for p in mock.admin("GET", "/admin/defence/presets").json()}
    assert set(presets) == {"none", "rate_limit", "rate_limit+pow", "rate_limit+pow+captcha", "all"}
    ev = mock.create()
    r = mock.admin("PATCH", f"/admin/events/{ev}/config", json={"defences": {"preset": "rate_limit+pow"}})
    layers = r.json()["config"]["defences"]["layers"]
    assert layers["pow"]["enabled"] and layers["rate_limit"]["enabled"] and not layers["captcha"]["enabled"]
    r = mock.admin("PATCH", f"/admin/events/{ev}/config", json={"defences": {"preset": "bogus"}})
    assert r.json()["code"] == "VALIDATION_ERROR"
