r"""Chaos harness: run a short load through the public edge, inject ONE fault, heal, check the invariants.

    .\infra\fd.ps1 chaos kill-replica          (one fault)
    .\infra\fd.ps1 chaos all                   (every fault in turn, one JSON file each + a summary)

Needs the stack in simulation mode with file mail (it mints tokens for made-up users):
    .\infra\fd.ps1 up -Edge nginx -Sim -Mail file

Per fault it writes infra/chaos/results/<fault>.json:
    {fault, recovery_seconds, requests_failed, invariants_passed, ...}
  requests_failed    transport errors + 5xx seen by the load generator during the whole run
  outage_seconds     first failed request -> last failed request (0 when nothing failed)
  recovery_seconds   heal -> first successful probe of a REAL write (an enter by a fresh user)
  invariants_passed  A's checker (/admin/events/{id}/invariants) AND our own end-to-end checks (see check_state)

The fault is a hard process kill / suspend, not a polite shutdown: nothing gets to clean up.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path

import httpx
import psycopg

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "infra" / "local"))
import pg  # noqa: E402
import run as runner  # noqa: E402

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8080")
ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "")}
SIM_KEY = os.environ.get("SIM_KEY", "")
EVENT = "11111111-1111-1111-1111-111111111111"
NS = uuid.UUID("5ca1e5ca-1e00-4000-8000-0000000000c4")
RESULTS = HERE / "results"
USERS = 6000


# ------------------------------------------------------------------------------------------------- the load
class Load:
    """Concurrent virtual users. Each takes a fresh identity, enters the draw, polls its status, repeats."""

    def __init__(self, uids: list[str], tokens: dict[str, str], workers: int = 16):
        self.uids, self.tokens, self.workers = uids, tokens, workers
        self.next_user = 0
        self.stop = False
        self.told_entered: set[str] = set()   # got 200/201 for /enter
        self.uncertain: set[str] = set()      # /enter ended in an error: may or may not have been recorded
        self.requests = 0
        self.failures: list[float] = []       # wall-clock time of each failed request (transport error or 5xx)
        self.by_code: dict[str, int] = {}

    def _hdr(self, uid: str, i: int) -> dict:
        return {"Authorization": f"Bearer {self.tokens[uid]}", "X-Device-Id": str(uuid.uuid5(NS, f"dev-{i}")), "X-Sim-Key": SIM_KEY,
                "X-Sim-Client-IP": f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}"}

    def _note(self, key: str) -> bool:
        self.requests += 1
        self.by_code[key] = self.by_code.get(key, 0) + 1
        bad = key.startswith("5") or not key.isdigit()
        if bad:
            self.failures.append(time.time())
        return bad

    async def _worker(self, c: httpx.AsyncClient) -> None:
        while not self.stop and self.next_user < len(self.uids):
            i = self.next_user
            self.next_user += 1
            uid = self.uids[i]
            h = self._hdr(uid, i)
            try:
                r = await c.post(f"/api/events/{EVENT}/enter", headers=h)
                bad = self._note(str(r.status_code))
                if r.status_code in (200, 201):
                    self.told_entered.add(uid)
                elif bad:
                    self.uncertain.add(uid)
            except httpx.HTTPError as exc:
                self._note(type(exc).__name__)
                self.uncertain.add(uid)
            for path in (f"/api/events/{EVENT}/status", f"/api/events/{EVENT}"):
                try:
                    r = await c.get(path, headers=h)
                    self._note(str(r.status_code))
                except httpx.HTTPError as exc:
                    self._note(type(exc).__name__)
            await asyncio.sleep(0.02)

    async def run(self) -> None:
        async with httpx.AsyncClient(base_url=BASE, timeout=20, limits=httpx.Limits(max_connections=self.workers + 4)) as c:
            await asyncio.gather(*[self._worker(c) for _ in range(self.workers)])


# --------------------------------------------------------------------------------------------- fault toggles
def _descendants(root_pid: int) -> list[int]:
    """Pids of the process tree under root_pid (Windows toolhelp snapshot)."""
    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k.CreateToolhelp32Snapshot(2, 0)
    pe = PE()
    pe.dwSize = ctypes.sizeof(PE)
    kids: dict[int, list[int]] = {}
    ok = k.Process32First(snap, ctypes.byref(pe))
    while ok:
        kids.setdefault(pe.th32ParentProcessID, []).append(pe.th32ProcessID)
        ok = k.Process32Next(snap, ctypes.byref(pe))
    k.CloseHandle(snap)
    out, todo = [root_pid], [root_pid]
    while todo:
        for ch in kids.get(todo.pop(), []):
            out.append(ch)
            todo.append(ch)
    return out


def _suspend(pids: list[int], on: bool) -> None:
    k, nt = ctypes.windll.kernel32, ctypes.windll.ntdll
    k.OpenProcess.restype = wintypes.HANDLE
    for pid in pids:
        h = k.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
        if h:
            (nt.NtSuspendProcess if on else nt.NtResumeProcess)(h)
            k.CloseHandle(h)


def pg_pids() -> list[int]:
    first = (pg.DATA / "postmaster.pid").read_text().splitlines()[0]
    return _descendants(int(first))


def pg_terminate_backends() -> int:
    env = runner.parse_env_file()
    dsn = f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}@127.0.0.1:{env['POSTGRES_PORT']}/{env['POSTGRES_DB']}"
    with psycopg.connect(dsn, autocommit=True) as c:
        rows = c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (env["POSTGRES_DB"],)).fetchall()
    return len(rows)


def replica_ports() -> list[int]:
    return [int(x.rsplit(":", 1)[1]) for x in runner.REPLICAS.read_text().split()]


# Each fault: inject() -> state, hold seconds, heal(state). A fault with no hold is instantaneous (the damage is the event).
def f_kill_replica():
    port = replica_ports()[1]
    runner.kill_proc(str(port))
    return {"killed": f"api-{port}"}, 6.0, lambda s: runner.heal("stub")


def f_kill_worker():
    runner.kill_proc("worker")
    return {"killed": "worker"}, 6.0, lambda s: runner.heal("stub")


def f_restart_redis():
    runner.kill_proc("redis")
    return {"killed": "redis"}, 8.0, lambda s: runner.heal("stub")


def f_pause_postgres():
    pids = pg_pids()
    _suspend(pids, True)
    return {"suspended_pids": len(pids)}, 5.0, lambda s: _suspend(pids, False)


def f_terminate_pg_connections():
    n = pg_terminate_backends()
    return {"backends_terminated": n}, 0.0, lambda s: None


def f_restart_edge():
    edge = runner.edge_mode()
    runner.kill_proc("nginx" if edge == "nginx" else "gateway")
    return {"killed": edge}, 4.0, lambda s: runner.heal("stub")


FAULTS = {
    "kill-replica": f_kill_replica,
    "kill-worker": f_kill_worker,
    "restart-redis": f_restart_redis,
    "pause-postgres": f_pause_postgres,
    "terminate-pg-connections": f_terminate_pg_connections,
    "restart-edge": f_restart_edge,
}


# ----------------------------------------------------------------------------------------------- the checks
async def probe_write(c: httpx.AsyncClient, uids: list[str], tokens: dict[str, str], spare: int, load: Load) -> tuple[float | None, int]:
    """Time until a fresh user can ENTER through the edge (a real write). Returns (seconds, probes_used)."""
    t0 = time.time()
    for k in range(120):
        uid, i = uids[spare + k], spare + k
        h = {"Authorization": f"Bearer {tokens[uid]}", "X-Device-Id": str(uuid.uuid5(NS, f"dev-{i}")), "X-Sim-Key": SIM_KEY,
             "X-Sim-Client-IP": f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}"}
        try:
            r = await c.post(f"/api/events/{EVENT}/enter", headers=h)
            if r.status_code in (200, 201):
                load.told_entered.add(uid)
                return round(time.time() - t0, 2), k + 1
            if r.status_code >= 500:
                load.uncertain.add(uid)
        except httpx.HTTPError:
            load.uncertain.add(uid)
        await asyncio.sleep(0.25)
    return None, 120


def check_state(uids: list[str], load: Load) -> list[tuple[str, bool, str]]:
    """Our own end-to-end checks against the database, on top of A's invariant checker."""
    out = []
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        rows = db.execute("SELECT user_id::text, count(*) FROM entries WHERE user_id = ANY(%s::uuid[]) GROUP BY 1", (uids,)).fetchall()
        weights = db.execute("SELECT count(*) FROM entries WHERE user_id = ANY(%s::uuid[]) AND weight NOT IN (1.00, 0.50, 0.25)", (uids,)).fetchone()[0]
    stored = {u for u, _ in rows}
    out.append(("nobody has two entries", all(n == 1 for _, n in rows), f"{sum(1 for _, n in rows if n > 1)} duplicated"))
    lost = load.told_entered - stored
    out.append(("every user told 'entered' has an entry (no acknowledged write was lost)", not lost, f"{len(lost)} lost"))
    extra = stored - load.told_entered
    out.append(("entries nobody was told about are only requests that ended in an error", extra <= load.uncertain | set(), f"{len(extra - load.uncertain)} unexplained"))
    out.append(("every weight is 1.0 / 0.5 / 0.25", weights == 0, f"{weights} bad"))
    return out


async def run_fault(name: str, warmup: float = 4.0, tail: float = 6.0) -> dict:
    if not SIM_KEY or not ADMIN["X-Admin-Token"]:
        raise SystemExit("run via: .\\infra\\fd.ps1 chaos <fault>   (needs the stack up with -Sim -Mail file)")
    uids = [str(uuid.uuid5(NS, f"chaos-{name}-{i}")) for i in range(USERS)]
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        db.execute("DELETE FROM entries WHERE user_id = ANY(%s::uuid[])", (uids,))
        with db.cursor() as cur:
            cur.executemany("INSERT INTO public.users (id, email, display_name) VALUES (%s, %s, 'Chaos') ON CONFLICT (id) DO NOTHING",
                            [(u, f"chaos-{name}-{i}@example-college.edu") for i, u in enumerate(uids)])
        db.commit()
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        (await c.patch(f"/api/admin/events/{EVENT}/config", json={"defences": {"preset": "none"}}, headers=ADMIN)).raise_for_status()
        await asyncio.sleep(2.0)
        tokens: dict[str, str] = {}
        for i in range(0, len(uids), 2000):
            tr = await c.post("/api/admin/sim/tokens", json={"user_ids": uids[i:i + 2000], "ttl_s": 1800}, headers=ADMIN)
            tr.raise_for_status()
            tokens.update(tr.json()["tokens"])

    load = Load(uids[:-200], tokens)  # the last 200 identities are reserved for recovery probes
    task = asyncio.create_task(load.run())
    await asyncio.sleep(warmup)
    before_req, before_fail = load.requests, len(load.failures)
    t_fault = time.time()
    state, hold, heal = FAULTS[name]()
    print(f"  [{name}] injected {state}; holding {hold:.0f} s under load")
    await asyncio.sleep(hold)
    t_heal = time.time()
    await asyncio.get_running_loop().run_in_executor(None, heal, state)
    heal_s = time.time() - t_heal
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as pc:
        rec, probes = await probe_write(pc, uids, tokens, len(uids) - 200, load)
    await asyncio.sleep(tail)
    load.stop = True
    await task

    await asyncio.sleep(1.5)  # decision-log batches
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        inv = (await c.get(f"/api/admin/events/{EVENT}/invariants", headers=ADMIN)).json()
        await c.patch(f"/api/admin/events/{EVENT}/config", json={"defences": {"preset": "none"}}, headers=ADMIN)
    checks = check_state(uids, load)
    checks.append(("A's invariant checker passes", inv.get("passed") is True, json.dumps(inv)[:200]))
    checks.append(("the system took writes again after healing", rec is not None, "no successful enter within 30 s"))
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        db.execute("DELETE FROM entries WHERE user_id = ANY(%s::uuid[])", (uids,))
        db.execute("DELETE FROM defence.decisions WHERE user_id = ANY(%s::uuid[])", (uids,))
        db.execute("DELETE FROM public.users WHERE id = ANY(%s::uuid[])", (uids,))
        db.commit()

    fails = [t for t in load.failures if t >= t_fault]
    result = {
        "fault": name, "state": state, "recovery_seconds": rec, "requests_failed": len(load.failures),
        "invariants_passed": all(ok for _, ok, _ in checks),
        "outage_seconds": round(max(fails) - min(fails), 2) if fails else 0.0,
        "requests_total": load.requests, "requests_during_fault": load.requests - before_req,
        "failures_by_kind": {k: v for k, v in load.by_code.items() if k.startswith("5") or not k.isdigit()},
        "status_codes": load.by_code, "held_seconds": hold, "heal_command_seconds": round(heal_s, 2), "edge": runner.edge_mode(),
        "users_told_entered": len(load.told_entered), "enter_outcome_unknown": len(load.uncertain),
        "checks": [{"name": n, "ok": ok, **({} if ok else {"detail": d})} for n, ok, d in checks],
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(result, indent=1))
    print(f"  [{name}] {load.requests} requests, {len(load.failures)} failed (outage {result['outage_seconds']} s), "
          f"recovered {rec} s after healing, invariants {'PASS' if result['invariants_passed'] else 'FAIL'}")
    for n, ok, d in checks:
        if not ok:
            print(f"      FAIL  {n}  [{d}]")
    return result


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fault", choices=[*FAULTS, "all"])
    a = ap.parse_args()
    names = list(FAULTS) if a.fault == "all" else [a.fault]
    runs = []
    for n in names:
        runs.append(await run_fault(n))
        await asyncio.sleep(3.0)  # let breakers close and caches settle between faults
    if len(runs) > 1:
        print(f"\n{'fault':<26}{'failed':>8}{'outage s':>10}{'recovery s':>12}   invariants")
        for r in runs:
            print(f"{r['fault']:<26}{r['requests_failed']:>8}{r['outage_seconds']:>10}{str(r['recovery_seconds']):>12}   {'PASS' if r['invariants_passed'] else 'FAIL'}")
        (RESULTS / "summary.json").write_text(json.dumps([{k: r[k] for k in ("fault", "recovery_seconds", "requests_failed", "invariants_passed", "outage_seconds")} for r in runs], indent=1))
    return 0 if all(r["invariants_passed"] for r in runs) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
