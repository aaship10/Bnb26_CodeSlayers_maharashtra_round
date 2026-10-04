"""`fdsim doctor`: validate every assumption the simulator makes about a target BEFORE a run, and say
exactly what is missing and who owns it. Exit code 0 only when a full run is possible.

Checks, in order: reachable -> which kind (mock/real) -> admin token works -> routes the run needs
(from the server's OpenAPI) -> real only: database reachable and schema as expected, users table
writable for provisioning, auth mode, defence layer endpoints, draw verifier configured.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from fairdrop_sim.adapters import real_db
from fairdrop_sim.adapters.capabilities import REQUIRED_FOR_FULL_RUN, Capabilities, probe

OK, WARN, FAIL = "OK", "WARN", "FAIL"
OWNER = {
    "draw": "A", "claim": "A", "reset": "A", "stats": "A", "invariants": "A", "stream": "A", "readyz": "A",
    "schedule": "A", "open": "A", "close": "A", "enter": "A", "status": "A",
    "defence_presets": "B", "defence_decisions": "B", "sim_tokens": "B", "defence_challenge": "B",
}
# Needed for a full run vs. nice to have. Defence endpoints only matter once a preset other than "none" is used.
NEEDED_FOR_DEFENCE_RUNS = ("defence_presets", "defence_decisions", "sim_tokens")
NICE_TO_HAVE = ("reset", "stats", "invariants", "readyz", "stream")


@dataclass
class Check:
    level: str
    name: str
    detail: str
    owner: str | None = None


@dataclass
class Report:
    base_url: str
    checks: list[Check] = field(default_factory=list)
    caps: Capabilities | None = None

    def add(self, level: str, name: str, detail: str, owner: str | None = None) -> None:
        self.checks.append(Check(level, name, detail, owner))

    @property
    def ok_for_full_run(self) -> bool:
        return not any(c.level == FAIL for c in self.checks)

    def render(self) -> str:
        w = max((len(c.name) for c in self.checks), default=10)
        lines = [f"fdsim doctor -> {self.base_url}"]
        for c in self.checks:
            who = f" [{c.owner}]" if c.owner else ""
            lines.append(f"  {c.level:<4} {c.name:<{w}}  {c.detail}{who}")
        n_fail = sum(c.level == FAIL for c in self.checks)
        n_warn = sum(c.level == WARN for c in self.checks)
        lines.append(f"\n  => {'READY for a full run' if self.ok_for_full_run else 'NOT ready for a full run'}"
                     f" ({n_fail} blocking, {n_warn} warning(s))")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"base_url": self.base_url, "ready": self.ok_for_full_run,
                "checks": [c.__dict__ for c in self.checks], "capabilities": self.caps.to_dict() if self.caps else None}


async def run_doctor(base_url: str, admin_token: str, sim_key: str | None = None, dsn: str | None = None) -> Report:
    rep = Report(base_url)
    caps = await probe(base_url)
    rep.caps = caps
    if not caps.reachable:
        rep.add(FAIL, "reachable", f"nothing answers /health or /healthz at {base_url}")
        return rep
    rep.add(OK, "reachable", f"{caps.kind} target, {caps.openapi_title or 'no title'} (routes via {caps.source})")
    for n in caps.notes:
        rep.add(WARN, "note", n)

    # admin token: any admin read that exists
    async with httpx.AsyncClient(base_url=base_url, timeout=8) as c:
        try:
            r = await c.get("/admin/events", headers={"X-Admin-Token": admin_token})
            if r.status_code in (200,):
                rep.add(OK, "admin token", "accepted by GET /admin/events")
            elif r.status_code in (401, 403):
                rep.add(FAIL, "admin token", f"rejected ({r.status_code}); set ADMIN_TOKEN / --admin-token", "B")
            else:
                rep.add(WARN, "admin token", f"GET /admin/events answered {r.status_code}; cannot confirm the token")
        except httpx.HTTPError as e:
            rep.add(FAIL, "admin token", f"request failed: {e}")

    for name in REQUIRED_FOR_FULL_RUN:
        if caps.has(name):
            rep.add(OK, f"route {name}", "present")
        else:
            rep.add(FAIL, f"route {name}", "MISSING: a full run cannot be completed without it", OWNER.get(name))
    for name in NICE_TO_HAVE:
        if not caps.has(name):
            lvl = WARN
            why = {"reset": "the real driver creates a fresh event per run instead",
                   "stats": "progress comes from the database instead",
                   "invariants": "only the simulator's own SQL check (I1-I7) runs",
                   "readyz": "needed by B's load balancer and chaos runs",
                   "stream": "SSE push is modelled by polling /status (documented limitation)"}[name]
            rep.add(lvl, f"route {name}", f"missing; {why}", OWNER.get(name))
    missing_def = [n for n in NEEDED_FOR_DEFENCE_RUNS if not caps.has(n)]
    if missing_def:
        rep.add(WARN, "defence endpoints", f"missing {', '.join(missing_def)}: runs with defence preset 'none' only "
                "(detection metrics and jwt token minting need these)", "B")
    else:
        rep.add(OK, "defence endpoints", "presets, decisions and sim tokens present")

    if caps.kind == "real":
        dsn = dsn or os.environ.get("FD_REAL_DSN")
        if not dsn:
            rep.add(FAIL, "database", "FD_REAL_DSN / --dsn not set: needed to provision simulated users and read entries", "C")
        else:
            try:
                missing = await asyncio.to_thread(real_db.validate_schema, dsn)
                if missing:
                    rep.add(FAIL, "db schema", f"missing columns the simulator reads: {', '.join(missing)}", "A")
                else:
                    rep.add(OK, "db schema", "every column in adapters/real_db.py REQUIRED_COLUMNS exists")
                n = await asyncio.to_thread(real_db.count_sim_users, dsn)
                rep.add(OK, "database", f"reachable; {n:,} simulated users already provisioned")
            except real_db.DbUnavailable as e:
                rep.add(FAIL, "database", str(e))
        if not caps.has("verifier"):
            rep.add(WARN, "draw verifier", "FD_VERIFY_CMD not set: draw_verified will be null, never a claimed True", "A")
        else:
            rep.add(OK, "draw verifier", "configured via FD_VERIFY_CMD")
        if sim_key is None:
            rep.add(WARN, "sim key", "SIM_KEY not set: fake client IPs and jwt sim tokens unavailable (fine for dev auth)", "B")
    return rep
