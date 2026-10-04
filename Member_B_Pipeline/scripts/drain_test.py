r"""Live graceful-shutdown test against the RUNNING stack (real processes, real gateway).

    .\infra\fd.ps1 up -Sim -Mail file          # simulation mode: tokens can be minted; no real email
    .\infra\fd.ps1 exec python scripts/drain_test.py

What it proves, with numbers:
  1. Draining replica R: /readyz flips to 503 straight away.
  2. A request already running on R when the drain starts still COMPLETES (even though R has stopped accepting).
  3. An open SSE stream on R receives `event: reconnect` and ends; reconnecting (a new stream) through the edge lands on a
     DIFFERENT replica.
  4. Meanwhile ordinary traffic through the gateway sees ZERO failed requests (no 5xx, no connection errors):
     that is the actual drain guarantee.
  5. R then exits, and the gateway keeps serving from the remaining replicas.
Afterwards it restores the replica count with `run.py scale`.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8080")
ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "")}
EVENT = "11111111-1111-1111-1111-111111111111"
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def served_by(value: str | None) -> str | None:
    """Port of the replica that finally answered. nginx lists every replica it tried ("a, b"); take the last."""
    return value.split(",")[-1].strip().rsplit(":", 1)[-1] if value else None


def replicas() -> list[int]:
    f = ROOT / ".local" / "run" / "replicas"
    return [int(ln.strip().rsplit(":", 1)[1]) for ln in f.read_text().splitlines() if ln.strip()]


async def port_open(port: int) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.5)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def main() -> int:
    ports = replicas()
    if len(ports) < 2:
        raise SystemExit("need at least 2 replicas: .\\infra\\fd.ps1 up -Sim -Mail file")
    uid = str(uuid.uuid4())
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{BASE}/api/admin/sim/tokens", json={"user_ids": [uid], "ttl_s": 600}, headers=ADMIN)
        if r.status_code != 200:
            raise SystemExit("minting tokens failed: start the stack in simulation mode: .\\infra\\fd.ps1 up -Sim -Mail file")
        auth = {"Authorization": f"Bearer {r.json()['tokens'][uid]}", "X-Device-Id": str(uuid.uuid4())}

        # 1. open an SSE stream through the gateway and note which replica serves it
        events: list[tuple[float, str]] = []
        stream_replica: dict[str, str] = {}

        async def sse():
            async with c.stream("GET", f"{BASE}/api/events/{EVENT}/stream", headers=auth, timeout=60) as resp:
                stream_replica["port"] = served_by(resp.headers.get("x-upstream")) or "?"
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        events.append((time.monotonic(), line.split(":", 1)[1].strip()))

        sse_task = asyncio.create_task(sse())
        for _ in range(50):
            if "port" in stream_replica:
                break
            await asyncio.sleep(0.1)
        target = int(stream_replica["port"])
        others = [p for p in ports if p != target]
        print(f"replicas {ports}; the stream is on :{target}; draining :{target}")

        # 2. long requests running on the target (sent straight to it: this is what the gateway had in flight)
        slow_started = time.monotonic()
        slow = [asyncio.create_task(c.get(f"http://127.0.0.1:{target}/dev/slow?seconds=6")) for _ in range(3)]

        # 3. steady traffic through the gateway for the whole episode
        outcomes: list[tuple[float, object]] = []
        stop = asyncio.Event()

        async def traffic():
            while not stop.is_set():
                t = time.monotonic()
                try:
                    rr = await c.get(f"{BASE}/api/events/{EVENT}/status", headers=auth, timeout=5)
                    outcomes.append((t, rr.status_code, served_by(rr.headers.get("x-upstream"))))
                except httpx.HTTPError as exc:
                    outcomes.append((t, repr(exc), None))
                await asyncio.sleep(0.03)

        tr = asyncio.create_task(traffic())
        await asyncio.sleep(0.5)

        # 4. start the drain on the target (the same code path as SIGTERM); stop accepting after 3 s
        t_drain = time.monotonic()
        d = await c.post(f"http://127.0.0.1:{target}/admin/defence/lifecycle/drain", json={"exit_after_s": 3}, headers=ADMIN)
        check("drain endpoint accepted", d.status_code == 200 and d.json()["draining"] is True, d.text)

        t_503 = None
        for _ in range(60):
            rz = await c.get(f"http://127.0.0.1:{target}/readyz")
            if rz.status_code == 503:
                t_503 = time.monotonic() - t_drain
                break
            await asyncio.sleep(0.05)
        check("/readyz flipped to 503 right away", t_503 is not None and t_503 < 1.0, f"{t_503}")
        print(f"      readiness flipped after {t_503 and round(t_503 * 1000)} ms")

        # 5. the SSE stream is told to reconnect and ends
        for _ in range(60):
            if any(name == "reconnect" for _, name in events):
                break
            await asyncio.sleep(0.1)
        got_hint = any(name == "reconnect" for _, name in events)
        check("open SSE stream received `event: reconnect`", got_hint, str(events))
        await asyncio.wait_for(sse_task, 10)
        check("the stream then ended cleanly (no hang)", sse_task.done())
        await asyncio.sleep(1.0)  # the `retry: 1000` hint: a browser waits this long before reconnecting
        # What a browser does after `event: reconnect`: open a NEW stream. The edge must not hand it to the draining replica
        # (the dev gateway skips it; nginx gets a 503 from it and retries elsewhere). A plain GET is NOT the test: a draining
        # replica legitimately keeps answering ordinary requests through its grace period.
        async with c.stream("GET", f"{BASE}/api/events/{EVENT}/stream", headers={**auth, "Accept": "text/event-stream"}) as r2:
            landed = served_by(r2.headers.get("x-upstream"))
            ok_status = r2.status_code == 200
        check("reconnecting (a NEW stream) through the edge lands on a DIFFERENT replica", ok_status and landed is not None and int(landed) in others,
              f"status {r2.status_code}, upstream {r2.headers.get('x-upstream')}")

        # 6. the long requests that were running on the target finish even though it stopped accepting
        done = await asyncio.gather(*slow, return_exceptions=True)
        took = time.monotonic() - slow_started
        ok = [isinstance(x, httpx.Response) and x.status_code == 200 for x in done]
        check("requests already in flight on the draining replica COMPLETED (3/3)", all(ok), f"{ok} {done}")
        print(f"      the 6 s requests finished after {took:.1f} s, after the replica stopped accepting new connections")

        # 7. the target exits; traffic keeps flowing
        gone = False
        for _ in range(100):
            if not await port_open(target):
                gone = True
                break
            await asyncio.sleep(0.2)
        check("the drained replica exited on its own", gone)
        await asyncio.sleep(1.0)
        stop.set()
        await tr

        bad = [o for o in outcomes if o[1] != 200]
        served_by_target_after_drain = [o for o in outcomes if o[0] > t_drain + 3.5 and o[2] == str(target)]
        print(f"      gateway traffic during the episode: {len(outcomes)} requests, {len(bad)} failed")
        check("ZERO failed requests through the gateway during the whole drain", not bad, str(bad[:5]))
        check("after it stopped accepting, the gateway sent nothing to it", not served_by_target_after_drain)
        check("the surviving replicas carried the traffic", {o[2] for o in outcomes[-10:]} <= {str(p) for p in others})

    print("\nrestoring the replica count...")
    subprocess.run([sys.executable, str(ROOT / "infra" / "local" / "run.py"), "scale", str(len(ports))], check=False)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} drain checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
