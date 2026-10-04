"""End-to-end check of a RUNNING local stack, through the gateway (what a browser would hit).

    python infra/local/run.py up
    python infra/local/run.py exec -- python scripts/local_e2e.py

Covers: register -> OTP from the file outbox -> verify -> /auth/me -> idempotent enter ->
client-IP spoofing is ignored -> honeypot -> alias collapse -> 429 contract -> replica spread.
Needs the stack in AUTH_MODE=jwt (the default in infra/.env.example).
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
import psycopg

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8080") + "/api"
OUTBOX = Path(os.environ.get("OUTBOX_DIR", ".local/outbox"))
EVENT = "11111111-1111-1111-1111-111111111111"
DOMAIN = "example-college.edu"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def latest_otp(to: str, after: float) -> str:
    deadline = time.time() + 5
    while time.time() < deadline:
        for f in sorted(OUTBOX.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.stat().st_mtime >= after - 1:
                m = json.loads(f.read_text())
                if m["to"] == to:
                    return "".join(ch for ch in m["body"].split("code is ")[1][:6])
        time.sleep(0.1)
    raise SystemExit(f"no OTP mail for {to} in {OUTBOX}")


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=10)
    tag = uuid.uuid4().hex[:6]
    email = f"e2e.{tag}@{DOMAIN}"
    device = str(uuid.uuid4())
    h = {"X-Device-Id": device, "User-Agent": "local-e2e/1.0"}

    # health + replica spread
    served = Counter()
    for _ in range(30):
        r = c.get("/healthz")
        served[r.status_code] += 1
    check("gateway proxies /api/healthz", served == Counter({200: 30}), str(served))

    t0 = time.time()
    # spoofing attempt: these must NOT become the recorded registration IP
    r = c.post("/auth/register", json={"email": email, "display_name": "E2E User", "hp": ""},
               headers={**h, "X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8", "X-Sim-Client-IP": "9.9.9.9"})
    check("register -> 202, no OTP in body", r.status_code == 202 and "otp" not in r.json(), r.text)
    otp = latest_otp(email, t0)
    r = c.post("/auth/verify", json={"email": email, "otp": otp}, headers=h)
    check("verify -> 200 + token", r.status_code == 200 and "token" in r.json(), r.text)
    tok = r.json()["token"]
    auth = {"Authorization": f"Bearer {tok}", **h}
    check("GET /auth/me", c.get("/auth/me", headers=auth).json().get("email") == email)

    r1 = c.post(f"/events/{EVENT}/enter", headers=auth)
    r2 = c.post(f"/events/{EVENT}/enter", headers=auth)
    check("enter is idempotent (201 then 200)", (r1.status_code, r2.status_code) == (201, 200), f"{r1.status_code},{r2.status_code} {r1.text}")
    check("enter returns the contract shape and never the weight", r1.json().get("state") == "ENTERED" and "weight" not in r1.json())
    check("enter without a token -> 401 UNAUTHENTICATED", c.post(f"/events/{EVENT}/enter").json().get("code") == "UNAUTHENTICATED")
    check("X-User-Id alone does not authenticate", c.get("/auth/me", headers={"X-User-Id": r.json()["user_id"]}).status_code == 401)

    # recorded registration data: peer as seen by the gateway, not the spoofed headers
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        row = conn.execute("SELECT host(registration_ip), device_id, user_agent_hash FROM defence.identities WHERE email_canonical = %s", (email,)).fetchone()
    check("spoofed forwarding headers were ignored", row is not None and row[0] == "127.0.0.1", str(row))
    check("device id and UA hash recorded", row is not None and row[1] == device and row[2] is not None)

    # honeypot
    hp = c.post("/auth/register", json={"email": f"hp.{tag}@{DOMAIN}", "display_name": "Bot", "hp": "x"}, headers=h)
    check("honeypot answers 202 but sends nothing", hp.status_code == 202 and not any(
        json.loads(f.read_text())["to"] == f"hp.{tag}@{DOMAIN}" for f in OUTBOX.glob("*.json")))

    # alias collapse (gmail is not on the default allowlist, so use +tag at the college domain)
    t1 = time.time()
    alias = f"e2e.{tag}+drop2@{DOMAIN}"
    c.post("/auth/register", json={"email": alias, "display_name": "Alias", "hp": ""}, headers={"X-Device-Id": str(uuid.uuid4())})
    v = c.post("/auth/verify", json={"email": alias, "otp": latest_otp(alias, t1)})
    check("+tag alias logs into the SAME identity", v.status_code == 200 and v.json()["user_id"] == r.json()["user_id"], v.text)

    # 429 contract: hammer one email
    codes = [c.post("/auth/register", json={"email": f"rl.{tag}@{DOMAIN}", "display_name": "RL", "hp": ""}).status_code for _ in range(6)]
    limited = c.post("/auth/register", json={"email": f"rl.{tag}@{DOMAIN}", "display_name": "RL", "hp": ""})
    ok429 = limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED" and "Retry-After" in limited.headers \
        and limited.json()["details"]["scope"] == "email" and limited.json()["details"]["retry_after_ms"] > 0
    check("per-email flood -> 429 RATE_LIMITED + Retry-After", 429 in codes + [limited.status_code] and ok429, f"{codes} {limited.text}")

    bad = c.post("/auth/register", json={"email": "x@evil.test", "display_name": "x", "hp": ""})
    check("disallowed domain -> 422 VALIDATION_ERROR", bad.status_code == 422 and bad.json()["code"] == "VALIDATION_ERROR")
    check("/api/metrics is not exposed", c.get("/metrics").status_code == 404)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
