"""Small attacks against a RUNNING stack, for MY layers' tests only (Member C owns the real simulator).

    python infra/local/run.py up
    python infra/local/run.py exec -- python scripts/smoke_attacks.py

Each attack is run with the defence OFF and then ON and the outcome is compared, so every
layer's effect is demonstrated rather than assumed. Stage 3 covers: flood from one identity,
flood from one IP with spoofed forwarding headers, honeypot, Sybil registrations from one device.
Stage 4 adds: bypass attempts against the PoW gate and replay of a solved challenge.
Stage 5 adds: a scripted vs a human-like registrant under the risk engine.
Exit code 1 if an expectation fails.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import hashlib

import httpx

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8080") + "/api"
ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "")}
OUTBOX = Path(os.environ.get("OUTBOX_DIR", ".local/outbox"))
EVENT = "11111111-1111-1111-1111-111111111111"
DOMAIN = "example-college.edu"
failures: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(name)


async def set_preset(c: httpx.AsyncClient, preset: str | dict) -> None:
    defences = {"preset": preset} if isinstance(preset, str) else preset
    r = await c.patch(f"/admin/events/{EVENT}/config", json={"defences": defences}, headers=ADMIN)
    r.raise_for_status()
    await asyncio.sleep(2.2)  # each replica caches the event config for ~1.5 s


async def login(c: httpx.AsyncClient, tag: str) -> dict:
    email = f"smoke.{tag}.{uuid.uuid4().hex[:6]}@{DOMAIN}"
    since = time.time()
    dev = {"X-Device-Id": str(uuid.uuid4())}
    r = await c.post("/auth/register", json={"email": email, "display_name": "Smoke", "hp": ""}, headers=dev)
    r.raise_for_status()
    for _ in range(50):
        for f in sorted(OUTBOX.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.stat().st_mtime >= since - 1:
                m = json.loads(f.read_text())
                if m["to"] == email:
                    otp = m["body"].split("code is ")[1][:6]
                    v = await c.post("/auth/verify", json={"email": email, "otp": otp}, headers=dev)
                    v.raise_for_status()
                    return {"Authorization": f"Bearer {v.json()['token']}", **dev}
        await asyncio.sleep(0.1)
    raise RuntimeError("OTP mail not found")


def tally(rs: list[httpx.Response]) -> str:
    return ", ".join(f"{k}x{v}" for k, v in sorted(Counter(r.status_code for r in rs).items()))


async def attack_identity_flood(c: httpx.AsyncClient) -> None:
    print("\n[1] flood /status from ONE identity (40 rapid requests)")
    h = await login(c, "flood")
    await set_preset(c, "none")
    off = [await c.get(f"/events/{EVENT}/status", headers=h) for _ in range(40)]
    await set_preset(c, "rate_limit")
    on = [await c.get(f"/events/{EVENT}/status", headers=h) for _ in range(40)]
    print(f"      layer OFF: {tally(off)}      layer ON: {tally(on)}")
    expect("OFF: every request served", {r.status_code for r in off} == {200})
    expect("ON: most of the flood is rejected with 429", sum(r.status_code == 429 for r in on) >= 30)
    r429 = next((r for r in on if r.status_code == 429), None)
    expect("429 follows the contract", r429 is not None and r429.json()["code"] == "RATE_LIMITED"
           and r429.json()["details"]["scope"] == "identity" and int(r429.headers["retry-after"]) >= 1)


async def attack_ip_flood_with_spoofing(c: httpx.AsyncClient) -> None:
    print("\n[2] flood from one IP, rotating X-Forwarded-For / X-Real-IP / X-Sim-Client-IP (900 requests, unauthenticated)")
    # A small per-IP bucket (50 burst, 1/s) so the outcome does not depend on how fast this machine is:
    # the DEFAULT per-IP limit (600 burst, 100/s) is intentionally generous for campus NATs.
    await set_preset(c, {"preset": "custom", "layers": {"rate_limit": {"enabled": True,
                    "limits": {"status": {"ip": {"capacity": 50, "refill_per_s": 1.0}}}}}})
    sem = asyncio.Semaphore(40)

    async def one(i: int) -> httpx.Response:
        async with sem:
            return await c.get(f"/events/{EVENT}/status", headers={
                "X-Forwarded-For": f"203.0.113.{i % 250}", "X-Real-IP": f"198.51.100.{i % 250}", "X-Sim-Client-IP": f"192.0.2.{i % 250}"})

    rs = await asyncio.gather(*[one(i) for i in range(900)])
    scopes = Counter((r.json().get("details") or {}).get("scope") for r in rs if r.status_code == 429)
    print(f"      result: {tally(rs)}   429 scopes: {dict(scopes)}")
    expect("spoofed headers did not earn extra budget: ~all but the first 50 + refill are limited by IP", scopes.get("ip", 0) >= 800)
    expect("shed before auth: the requests that got through were answered 401, never 5xx", {r.status_code for r in rs} <= {401, 429})


async def attack_honeypot(c: httpx.AsyncClient) -> None:
    print("\n[3] honeypot: a bot fills the hidden field")
    email = f"smoke.hp.{uuid.uuid4().hex[:6]}@{DOMAIN}"
    r = await c.post("/auth/register", json={"email": email, "display_name": "Bot", "hp": "http://spam"})
    await asyncio.sleep(0.6)
    sent = any(json.loads(f.read_text())["to"] == email for f in OUTBOX.glob("*.json"))
    expect("looks like a success to the bot (202)", r.status_code == 202)
    expect("but no email was sent and nothing was created", not sent)


async def attack_sybil_one_device(c: httpx.AsyncClient) -> None:
    print("\n[4] Sybil registrations from one device id (8 different emails)")
    dev = {"X-Device-Id": str(uuid.uuid4())}
    rs = []
    for i in range(8):
        rs.append(await c.post("/auth/register", json={"email": f"sybil{uuid.uuid4().hex[:5]}@{DOMAIN}", "display_name": "S", "hp": ""}, headers=dev))
    print(f"      result: {tally(rs)}")
    expect("the first 5 pass, the rest are limited by device", [r.status_code for r in rs] == [202] * 5 + [429] * 3)
    expect("limited by the DEVICE dimension", rs[-1].json()["details"]["scope"] == "device")


def solve(prefix: str, bits: int) -> str:
    """Tiny independent solver (hashlib only)."""
    base = hashlib.sha256(f"{prefix}:".encode())
    n = 0
    while True:
        h = base.copy()
        h.update(str(n).encode())
        if 256 - int.from_bytes(h.digest(), "big").bit_length() >= bits:
            return str(n)
        n += 1


async def attack_challenge_bypass(c: httpx.AsyncClient) -> None:
    print("\n[5] bypass attempts against the proof-of-work gate (5 fresh people per variant)")
    users = [await login(c, f"bypass{i}") for i in range(5)]

    async def tries(headers_for) -> list[httpx.Response]:
        out = []
        for h in users:
            r = await c.post(f"/events/{EVENT}/enter", headers=h)  # the challenge we are 'given'
            ch = (r.json().get("details") or {}).get("challenge") if r.status_code == 403 else None
            out.append(await c.post(f"/events/{EVENT}/enter", headers={**h, **headers_for(ch, h)}))
        return out

    variants = {
        "no solution at all": lambda ch, h: {},
        "random nonce": lambda ch, h: {"X-Challenge-Id": ch["id"], "X-Challenge-Solution": "12345"} if ch else {},
        "forged id, difficulty 0": lambda ch, h: {"X-Challenge-Id": ch["id"].replace(f".{ch['pow']['difficulty_bits']}.", ".0.", 1), "X-Challenge-Solution": "0"} if ch else {},
        "garbage id": lambda ch, h: {"X-Challenge-Id": "x", "X-Challenge-Solution": "0"},
    }
    await set_preset(c, "none")
    off_users = [await login(c, f"off{i}") for i in range(5)]
    off = [await c.post(f"/events/{EVENT}/enter", headers=h) for h in off_users]
    print(f"      layer OFF, no solution: {tally(off)}  (everyone gets in)")
    expect("OFF: entering without solving is allowed (nothing to bypass)", {r.status_code for r in off} == {201})

    # PoW only (no rate limit) so that THIS attack isolates the proof-of-work gate
    await set_preset(c, {"preset": "custom", "layers": {"pow": {"enabled": True, "mode": "always"}}})
    for name, fn in variants.items():
        rs = await tries(fn)
        print(f"      layer ON, {name:<26}: {tally(rs)}")
        expect(f"ON: '{name}' creates no entry", all(r.status_code == 403 for r in rs))
    rs = await tries(lambda ch, h: {"X-Challenge-Id": ch["id"], "X-Challenge-Solution": solve(ch["pow"]["prefix"], ch["pow"]["difficulty_bits"])})
    print(f"      layer ON, honest solver        : {tally(rs)}")
    expect("ON: an honest solver gets in", {r.status_code for r in rs} == {201})

    # replay: a solved challenge belongs to ONE person
    a, b = (await login(c, "replayA")), (await login(c, "replayB"))
    ch = (await c.post(f"/events/{EVENT}/enter", headers=a)).json()["details"]["challenge"]
    sol = {"X-Challenge-Id": ch["id"], "X-Challenge-Solution": solve(ch["pow"]["prefix"], ch["pow"]["difficulty_bits"])}
    ok = await c.post(f"/events/{EVENT}/enter", headers={**a, **sol})
    stolen = await c.post(f"/events/{EVENT}/enter", headers={**b, **sol})
    print(f"      replay: owner {ok.status_code}, someone else with the same solution {stolen.status_code} {stolen.json().get('code')}")
    expect("replayed solution works only for the person it was issued to", ok.status_code == 201 and stolen.status_code == 403 and stolen.json()["code"] == "CHALLENGE_REQUIRED")


async def login_as(c: httpx.AsyncClient, tag: str, user_agent: str, otp_delay_s: float, local: str | None = None) -> dict:
    """Register and verify with a given User-Agent, waiting `otp_delay_s` before entering the code
    (a person reads an email first; a script does not)."""
    email = f"{local or f'smoke.{tag}.{uuid.uuid4().hex[:6]}'}@{DOMAIN}"
    since = time.time()
    h = {"X-Device-Id": str(uuid.uuid4()), "User-Agent": user_agent, "Accept-Language": "en-IN"}
    (await c.post("/auth/register", json={"email": email, "display_name": "Smoke", "hp": ""}, headers=h)).raise_for_status()
    for _ in range(80):
        for f in sorted(OUTBOX.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.stat().st_mtime >= since - 1:
                m = json.loads(f.read_text())
                if m["to"] == email:
                    await asyncio.sleep(otp_delay_s)
                    v = await c.post("/auth/verify", json={"email": email, "otp": m["body"].split("code is ")[1][:6]}, headers=h)
                    v.raise_for_status()
                    return {"Authorization": f"Bearer {v.json()['token']}", **h, "_uid": v.json()["user_id"]}
        await asyncio.sleep(0.1)
    raise RuntimeError("OTP mail not found")


async def attack_risk_engine(c: httpx.AsyncClient) -> None:
    print("\n[6] risk engine: a scripted registrant vs a human-like one, same event, same config")
    import random
    import string

    # a person's address looks like a name (letters only here), not like random characters
    name_like = "priya." + "".join(random.choices(string.ascii_lowercase, k=6))
    human = await login_as(c, "human", "Mozilla/5.0 (Windows NT 10.0) Chrome/126", otp_delay_s=9.0, local=name_like)
    bot = await login_as(c, "bot", "python-requests/2.31", otp_delay_s=0.0)

    async def outcome(h: dict) -> tuple[str, dict | None]:
        uid = h.pop("_uid")
        trail = []
        hdr = dict(h)
        for _ in range(4):
            r = await c.post(f"/events/{EVENT}/enter", headers=hdr)
            if r.status_code == 201:
                trail.append("entered")
                break
            ch = r.json()["details"]["challenge"]
            trail.append(ch["type"])
            if ch["type"] == "pow":
                hdr = {**h, "X-Challenge-Id": ch["id"], "X-Challenge-Solution": solve(ch["pow"]["prefix"], ch["pow"]["difficulty_bits"])}
            else:
                hdr = {**h, "X-Challenge-Id": ch["id"], "X-Challenge-Solution": "mock-captcha-ok"}
        await asyncio.sleep(1.0)  # the log is written in batches (<= 0.5 s) by the replica that served the request
        d, cursor = [], 0
        while cursor is not None:  # the log grows with every run: walk ALL pages (oldest first), never just the first one
            page = (await c.get("/admin/defence/decisions", params={"event_id": EVENT, "limit": 1000, "cursor": cursor}, headers=ADMIN)).json()
            d += page["decisions"]
            cursor = page["next_cursor"]
        mine = [x for x in d if x["user_id"] == uid and x["action"] == "ALLOW"]
        return " -> ".join(trail), (mine[-1] if mine else None)

    await set_preset(c, {"preset": "custom", "layers": {"pow": {"enabled": True, "mode": "risk"}, "captcha": {"enabled": True, "mode": "risk"},
                                                         "signals": {"enabled": True}, "risk": {"enabled": True}}})
    h_path, h_dec = await outcome(human)
    b_path, b_dec = await outcome(bot)
    print(f"      human-like registrant: {h_path}   risk {h_dec and h_dec['score']}")
    print(f"      scripted registrant:   {b_path}   risk {b_dec and b_dec['score']}  signals {[s['name'] for s in (b_dec or {}).get('signals', {}).get('signals', [])]}")
    expect("human-like registrant is not challenged at all", h_path == "entered" and h_dec is not None and h_dec["score"] < 0.30)
    expect("scripted registrant is challenged (PoW, then CAPTCHA) because of its risk score", b_path == "pow -> captcha -> entered" and (b_dec or {}).get("score", 0) >= 0.30)
    await set_preset(c, "none")
    off_user = await login_as(c, "bot-off", "python-requests/2.31", otp_delay_s=0.0)
    off_user.pop("_uid")
    r = await c.post(f"/events/{EVENT}/enter", headers=off_user)
    print(f"      same scripted registrant with every layer OFF: HTTP {r.status_code} (no challenge)")
    expect("OFF: the same scripted registrant just enters", r.status_code == 201)


async def main() -> int:
    if not ADMIN["X-Admin-Token"]:
        raise SystemExit("ADMIN_TOKEN not set: run via `python infra/local/run.py exec -- python scripts/smoke_attacks.py`")
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        try:
            await attack_identity_flood(c)
            await attack_ip_flood_with_spoofing(c)
            await attack_honeypot(c)
            await attack_sybil_one_device(c)
            await attack_challenge_bypass(c)
            await attack_risk_engine(c)
        finally:
            await set_preset(c, "none")
    print(f"\n{'ALL SMOKE ATTACKS BEHAVED AS EXPECTED' if not failures else 'FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
