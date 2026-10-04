"""Target drivers: everything the runner needs from a Fair Drop server, behind one interface.

  MockDriver    the in-memory dev double (mock_server). Schedules the window by wall clock, can reset an
                event for the next repeat, exposes /__mock/ analytics. Never evidence.
  AEngineDriver Member A's real backend + its Postgres. Window opened and closed by A's manual admin
                overrides at the planned instants; a FRESH event per run (A has no reset, and a new
                event id is the clean way to separate runs: B's R27 recommends the same).

The runner never touches a route or a column directly, so a change in A's or B's API touches this
file, capabilities.py and real_db.py only.
"""
from __future__ import annotations

import asyncio
import math
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from fairdrop_sim.adapters import real_db
from fairdrop_sim.adapters.api_adapter import AdminApi, AdminError
from fairdrop_sim.adapters.capabilities import Capabilities, probe
from fairdrop_sim.adapters.db_adapter import ENTRY_COLUMNS, entries_from_mock_export, validate_entries
from fairdrop_sim.crowd.population import Bots, Humans
from fairdrop_sim.models.scenario import Scenario

DECISIONS_PAGE = 1_000
DECISIONS_CAP = 500_000
WINDOW_PLACEHOLDER_H = (1, 2)  # real events are created with a far-future window, then opened/closed by override


class TargetMismatch(RuntimeError):
    pass


class DriverUnavailable(RuntimeError):
    """The target cannot do what this run needs. Message is showable as-is."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Driver:
    kind = "unknown"  # "mock" | "real"
    manual_window = False  # True: the coordinator must open and close the window at the planned instants

    def __init__(self, admin: AdminApi, caps: Capabilities):
        self.admin, self.caps = admin, caps
        self.event_id: str | None = None

    # --- lifecycle -------------------------------------------------------------------------
    async def provision(self, humans: Humans, bots: list[Bots]) -> dict[str, Any]:
        raise NotImplementedError

    async def prepare_event(self, sc: Scenario, run_index: int, seed_hex: str, hint: str) -> str:
        raise NotImplementedError

    async def schedule(self, wall_open: float, window_s: float, margin_s: float) -> None:
        """Mock: schedule by wall clock. Real: nothing (manual open/close)."""

    async def open_window(self) -> None:
        """Real only."""

    async def close_window(self) -> None:
        raise NotImplementedError

    async def draw(self, sc: Scenario) -> dict[str, Any] | None:
        raise NotImplementedError

    async def progress(self) -> dict[str, Any]:
        raise NotImplementedError

    # --- after the load --------------------------------------------------------------------
    async def invariants(self) -> dict[str, Any]:
        raise NotImplementedError

    async def server_view(self) -> dict[str, Any]:
        raise NotImplementedError

    def environment(self) -> dict[str, Any]:
        return {"driver": type(self).__name__, "kind": self.kind, "base_url": self.caps.base_url,
                "python": sys.version.split()[0], "capabilities": self.caps.to_dict()}


# =============================================================================== mock


def _real_defences(defences: dict[str, Any]) -> dict[str, Any]:
    """B's schema is strict: a named preset may not carry layer overrides (they would make the experiment
    label lie), and its parameter names are B's own. Layer overrides in a scenario use the mock's toy
    parameter names, so for the real target a named preset is sent by name only and B expands it."""
    if defences.get("preset", "none") != "custom":
        return {"preset": defences.get("preset", "none")}
    return defences


class MockDriver(Driver):
    kind = "mock"

    async def provision(self, humans: Humans, bots: list[Bots]) -> dict[str, Any]:
        n = len(humans) + sum(len(b) for b in bots)
        return {"provisioned": n, "via": "mock (accepts any well-formed id)"}

    async def prepare_event(self, sc: Scenario, run_index: int, seed_hex: str, hint: str) -> str:
        defences = sc.event.defences.model_dump(exclude_none=True)
        if await self.admin.get_event(hint) is None:
            await self.admin.create_event({
                "id": hint, "name": f"SIM {sc.name}", "inventory": sc.event.inventory, "mode": sc.event.mode,
                "window_seconds": sc.event.window_seconds, "claim_ttl_seconds": sc.event.claim_ttl_seconds,
                "server_seed_hex": seed_hex, "config": {"defences": defences}})
        else:
            await self.admin.reset(hint, seed_hex)
            await self.admin.set_defences(hint, defences)
        self.event_id = hint
        return hint

    async def schedule(self, wall_open: float, window_s: float, margin_s: float) -> None:
        iso = _iso(datetime.fromtimestamp(wall_open - margin_s, timezone.utc))
        await self.admin.schedule(self.event_id, iso, window_s + 2 * margin_s)

    async def close_window(self) -> None:
        ev = await self.admin.get_event(self.event_id) or {}
        if ev.get("phase") == "OPEN":
            await self.admin.close(self.event_id)

    async def draw(self, sc: Scenario) -> dict[str, Any] | None:
        if sc.event.mode != "LOTTERY":
            return None
        return (await self.admin.draw(self.event_id)).get("draw")

    async def progress(self) -> dict[str, Any]:
        st = await self.admin.stats(self.event_id)
        reqs = st.get("requests")
        return {"phase": st.get("phase"), "entries": st.get("entries", 0), "states": st.get("states", {}),
                "requests": sum(v for k, v in reqs.items() if k in ("enter", "status", "claim"))
                if isinstance(reqs, dict) else None}

    async def invariants(self) -> dict[str, Any]:
        return await self.admin.invariants(self.event_id)

    async def server_view(self) -> dict[str, Any]:
        export = await self.admin.mock_export(self.event_id)
        return {"entries": entries_from_mock_export(export), "decisions": export.get("decisions", []),
                "verify": await self.admin.mock_verify(self.event_id)}


# =============================================================================== A's real engine


class AEngineDriver(Driver):
    kind = "real"
    manual_window = True

    def __init__(self, admin: AdminApi, caps: Capabilities, dsn: str | None):
        super().__init__(admin, caps)
        self.dsn = dsn

    def _need_db(self) -> str:
        if not self.dsn:
            raise DriverUnavailable(
                "The real target needs database access for provisioning simulated users and for the "
                "analytics view. Set FD_REAL_DSN (e.g. postgresql://user:pw@host:5432/fairdrop); use a "
                "read-only role for analytics where you can.")
        return self.dsn

    async def provision(self, humans: Humans, bots: list[Bots]) -> dict[str, Any]:
        dsn = self._need_db()
        missing = await asyncio.to_thread(real_db.validate_schema, dsn)
        if missing:
            raise DriverUnavailable("The target database does not match the schema the simulator reads: missing "
                                    f"{', '.join(missing)} (adapters/real_db.py REQUIRED_COLUMNS).")
        users = [(u, "human") for u in humans.user_ids]
        for b in bots:
            users += [(u, "bot") for u in b.user_ids]
        new = await asyncio.to_thread(real_db.insert_users, dsn, users)
        return {"provisioned": len(users), "new_rows": new, "via": "direct SQL into users (sim_label set)"}

    async def prepare_event(self, sc: Scenario, run_index: int, seed_hex: str, hint: str) -> str:
        lo, hi = WINDOW_PLACEHOLDER_H
        now = datetime.now(timezone.utc)
        ev = sc.event
        body: dict[str, Any] = {
            "name": f"SIM {sc.name} r{run_index:02d}"[:200], "inventory": ev.inventory, "mode": ev.mode,
            "window_opens_at": _iso(now + timedelta(hours=lo)), "window_closes_at": _iso(now + timedelta(hours=hi)),
            "claim_ttl_seconds": max(1, math.ceil(ev.claim_ttl_seconds)),
            "claim_phase_seconds": max(1, math.ceil(sc.load.claim_phase_s or 3 * ev.claim_ttl_seconds)),
            "config": {"defences": _real_defences(ev.defences.model_dump(exclude_none=True))},
        }
        created = await self.admin.call("create event", "POST", "/admin/events", json=body)
        self.event_id = created["id"]
        await self.admin.call("schedule", "POST", f"/admin/events/{self.event_id}/schedule", json={})
        return self.event_id

    async def open_window(self) -> None:
        await self.admin.call("open", "POST", f"/admin/events/{self.event_id}/open", json={})

    async def close_window(self) -> None:
        await self.admin.call("close", "POST", f"/admin/events/{self.event_id}/close", json={})

    async def draw(self, sc: Scenario) -> dict[str, Any] | None:
        if sc.event.mode != "LOTTERY" or not self.caps.has("draw"):
            return None
        r = await self.admin.call("draw", "POST", f"/admin/events/{self.event_id}/draw", json={})
        return r.get("draw") if isinstance(r, dict) else None

    async def progress(self) -> dict[str, Any]:
        return await asyncio.to_thread(real_db.state_counts, self._need_db(), self.event_id)

    async def invariants(self) -> dict[str, Any]:
        mine = await asyncio.to_thread(real_db.invariants, self._need_db(), self.event_id)
        if not self.caps.has("invariants"):
            return mine
        # both must agree it is clean: A's own checker is authoritative, ours is independent
        theirs = await self.admin.call("invariants", "GET", f"/admin/events/{self.event_id}/invariants")
        passed = bool(mine["passed"]) and bool(theirs.get("passed"))
        return {"passed": passed, "checks": {**mine["checks"], **{f"a_{k}": v for k, v in
                (theirs.get("checks") if isinstance(theirs.get("checks"), dict) else theirs).items()
                if isinstance(v, int) and not isinstance(v, bool)}}, "source": "c_sql(I1-I7)+a_endpoint"}

    async def server_view(self) -> dict[str, Any]:
        dsn = self._need_db()
        df = await asyncio.to_thread(real_db.entries_frame, dsn, self.event_id)
        validate_entries_real(df)
        return {"entries": df, "decisions": await self._decisions(), "verify": await self._verify()}

    async def _decisions(self) -> list[dict[str, Any]]:
        if not self.caps.has("defence_decisions"):
            return []
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(out) < DECISIONS_CAP:
            params: dict[str, Any] = {"event_id": self.event_id, "limit": DECISIONS_PAGE}
            if cursor:
                params["cursor"] = cursor
            page = await self.admin.call("decisions", "GET", "/admin/defence/decisions", params=params)
            items = page.get("items", page.get("decisions", [])) if isinstance(page, dict) else page
            out += list(items)
            cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if not cursor or not items:
                break
        return out

    async def _verify(self) -> dict[str, Any] | None:
        """A's standalone verifier: FD_VERIFY_CMD with {event_id}, run in FD_VERIFY_CWD. None when
        not configured (draw_verified is then null, never a claimed True)."""
        cmd = os.environ.get("FD_VERIFY_CMD")
        if not cmd:
            return None
        argv = split_command(cmd.format(event_id=self.event_id))
        proc = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, text=True, timeout=120,
            cwd=os.environ.get("FD_VERIFY_CWD") or None)
        return {"verified": proc.returncode == 0, "exit_code": proc.returncode,
                "output": (proc.stdout or proc.stderr)[-400:]}


def split_command(cmd: str) -> list[str]:
    """shlex.split that also works for Windows paths: posix mode treats backslashes as escapes, and
    posix=False keeps the quotes around a quoted token, so split non-posix then strip the quotes."""
    if os.name != "nt":
        return shlex.split(cmd)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in shlex.split(cmd, posix=False)]


def validate_entries_real(df) -> None:
    """The real frame has NULL draw_state until a draw exists, which the shared validator allows."""
    for c in ENTRY_COLUMNS:
        if c not in df.columns:
            raise DriverUnavailable(f"real entries view is missing column {c!r}")
    validate_entries(df)


# =============================================================================== factory


async def make_driver(base_url: str, admin_token: str, expected_target: str, dsn: str | None = None) -> Driver:
    """Probe the server, check it is the kind of target the scenario asked for, and return its driver."""
    admin = AdminApi(base_url, admin_token)
    caps = await probe(base_url)
    if not caps.reachable:
        await admin.aclose()
        raise DriverUnavailable(f"nothing is answering at {base_url}")
    if caps.kind != expected_target:
        await admin.aclose()
        raise TargetMismatch(f"scenario says target={expected_target!r} but the server at {base_url} is {caps.kind!r}")
    if caps.kind == "mock":
        return MockDriver(admin, caps)
    return AEngineDriver(admin, caps, dsn if dsn is not None else os.environ.get("FD_REAL_DSN"))
