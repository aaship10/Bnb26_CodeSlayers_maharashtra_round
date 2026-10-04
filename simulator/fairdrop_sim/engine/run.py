"""Execute ONE run of a scenario against a target and write its artifacts.

Timeline (perf_counter seconds; t_open = window opens):
    now ........ t_open = now + lead_s ......... t_close = t_open + W
                 t_draw = t_close + draw_delay_s  (coordinator closes if needed, then draws)
                 t_end  = t_draw + claim_phase_s  (users stop sending new requests)

The target is reached through a Driver (adapters/driver.py): the in-memory mock schedules the window
by wall clock; Member A's real engine has its window opened and closed by the coordinator at these
instants through A's manual admin overrides. The mock window is padded by CLOCK_MARGIN_S at each end
so the 15.6 ms Windows wall-clock granularity cannot bounce an on-time first or last arrival.

An engine that cannot draw yet (A's stage 2) is run in ENTRY-ONLY mode: the load, latency and
availability numbers are real, the claim phase is skipped, and the run is marked so no fairness
number is ever published from it (metrics.aggregate refuses it).

Timed hooks (chaos injection) fire once at a chosen offset after the window opens.
Stage 5 wraps this in repeats, ablations and result aggregation.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from fairdrop_sim.adapters.api_adapter import AdminError
from fairdrop_sim.adapters.driver import Driver, DriverUnavailable, TargetMismatch, make_driver  # noqa: F401
from fairdrop_sim.bots.cost import CostAccount
from fairdrop_sim.crowd.population import BOT, HUMAN, build_bots, build_humans
from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.engine.shard import ShardSpec, high_res_timer, run_shard, shard_main, warm_up
from fairdrop_sim.engine.summary import summarize
from fairdrop_sim.models.scenario import Scenario
from fairdrop_sim.seeds import derive_seed, derive_seed_hex

CLOCK_MARGIN_S = 0.05
OPEN_MARGIN_S = 0.25  # real engine: open the window this long before t_open, so entries never bounce
CONTROL_RETRY_S = 45.0  # open/close/draw ride out a target outage this long (chaos runs), then fail the run
ENTRY_ONLY_CLAIM_S = 3.0  # an engine without a draw has nothing to claim; just let stragglers finish
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "results" / "runs"


@dataclass(frozen=True)
class TargetConfig:
    base_url: str = "http://127.0.0.1:8200"
    admin_token: str = "dev-admin-token"
    sim_key: str | None = "dev-sim-key"
    dsn: str | None = None  # real target: Postgres DSN for provisioning + analytics (else FD_REAL_DSN)


@dataclass
class TimedHook:
    """Call `fn` once, `at_s` seconds after the window opens (chaos injection). The outcome is
    recorded in the run summary; a hook that raises is reported, it does not abort the run."""

    name: str
    at_s: float
    fn: Callable[[], Awaitable[Any]]


def default_event_id(sc: Scenario) -> str:
    e = sc.event
    h = hashlib.sha256(f"{e.inventory}|{e.mode}|{e.window_seconds}|{e.claim_ttl_seconds}".encode()).hexdigest()[:6]
    return sc.event_id or f"evt_sim_{sc.name}_{h}"[:64]


async def _retry(fn: Callable[[], Awaitable[Any]], what: str, log: Callable[[str], None],
                 info: dict[str, Any]) -> Any:
    """Run a control-plane call, retrying transport failures and 5xx for up to CONTROL_RETRY_S.
    Safe because the engine's open/close/draw are idempotent. Every retry is counted in the summary,
    so a run that needed them says so. 4xx (a real refusal) is never retried."""
    deadline = time.perf_counter() + CONTROL_RETRY_S
    attempt = 0
    while True:
        try:
            return await fn()
        except (httpx.TransportError, AdminError) as e:
            if isinstance(e, AdminError) and e.status < 500:
                raise
            attempt += 1
            info["control_retries"] = info.get("control_retries", 0) + 1
            if time.perf_counter() > deadline:
                raise
            if attempt == 1:
                log(f"  {what}: target not answering ({type(e).__name__}); retrying for up to {CONTROL_RETRY_S:.0f}s")
            await asyncio.sleep(min(1.0, 0.2 * attempt))


async def execute_run(sc: Scenario, run_index: int, target: TargetConfig, out_dir: Path = DEFAULT_OUT,
                      log: Callable[[str], None] = print,
                      progress_cb: Callable[[dict[str, Any]], None] | None = None,
                      hooks: list[TimedHook] | None = None) -> dict[str, Any]:
    """`progress_cb` (optional) is called about once a second with
    {t_s, phase, entries, states, requests?, planned_s}: t_s is seconds since the window
    opened (negative during the lead-in), planned_s the planned run length after opening."""
    run_seed = derive_seed(sc.seed, "run", run_index)
    seed_hex = derive_seed_hex(sc.seed, "run", run_index)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{sc.name}-r{run_index:02d}-{stamp}"
    procs = sc.load.procs
    W, ttl = sc.event.window_seconds, sc.event.claim_ttl_seconds

    driver = await make_driver(target.base_url, target.admin_token, sc.target, target.dsn)
    pool: ProcessPoolExecutor | None = None
    shard_futs: list[asyncio.Future] = []
    try:
        caps = driver.caps
        entry_only = driver.kind == "real" and sc.event.mode == "LOTTERY" and not caps.draw
        claim_phase = sc.load.claim_phase_s or 3 * ttl
        if entry_only:
            claim_phase = min(claim_phase, ENTRY_ONLY_CLAIM_S)
            log(f"[{run_id}] ENTRY-ONLY: the target has no draw endpoint ({', '.join(caps.missing()) or 'n/a'} "
                "missing), so no seats are allocated and no fairness number will be published")

        humans = build_humans(sc.legit.count, sc.legit.nat_groups, sc.seed)
        bots_by_attacker = {
            ai: build_bots(ai, a.identities, a.ips, sc.seed, a.shared_device if a.shared_device is not None else False)
            for ai, a in enumerate(sc.attackers)
        }
        provisioned = await driver.provision(humans, list(bots_by_attacker.values()))
        event_id = await driver.prepare_event(sc, run_index, seed_hex, default_event_id(sc))

        if procs > 1:
            pool = ProcessPoolExecutor(max_workers=procs)
            loop = asyncio.get_running_loop()
            await asyncio.gather(*(loop.run_in_executor(pool, warm_up) for _ in range(procs)))

        now = time.perf_counter()
        wall_now = time.time()
        t_open = now + sc.load.lead_s
        t_close = t_open + W
        t_draw = t_close + sc.load.draw_delay_s
        t_end = t_draw + claim_phase
        await driver.schedule(wall_now + sc.load.lead_s, W, CLOCK_MARGIN_S)
        log(f"[{run_id}] {driver.kind} event {event_id} opens in {sc.load.lead_s:.1f}s; window {W:.0f}s, "
            f"draw at +{W + sc.load.draw_delay_s:.0f}s, run ends at +{W + sc.load.draw_delay_s + claim_phase:.0f}s; "
            f"{len(humans):,} legit users over {procs} shard(s)")

        specs = [
            ShardSpec(scenario=sc.model_dump(mode="json"), run_index=run_index, run_seed=run_seed, run_tag=run_id,
                      base_url=target.base_url, event_id=event_id, admin_token=target.admin_token,
                      sim_key=target.sim_key, shard=k, nshards=procs, t_open=t_open, t_close=t_close,
                      t_draw=t_draw, t_end=t_end)
            for k in range(procs)
        ]
        if pool is not None:
            loop = asyncio.get_running_loop()
            shard_futs = [loop.run_in_executor(pool, run_shard, s) for s in specs]
        else:
            shard_futs = [asyncio.ensure_future(shard_main(specs[0]))]

        draw_info = await _coordinate(driver, sc, t_open, t_close, t_draw, shard_futs, log, progress_cb,
                                      t_end - t_open, hooks or [])
        shard_results = await asyncio.gather(*shard_futs)

        invariants = await driver.invariants()
        server = await driver.server_view()
        stats = await driver.progress()
    finally:
        for f in shard_futs:  # a cancelled/failed run must not leave load running against the target
            if not f.done():
                f.cancel()
        await driver.admin.aclose()
        if pool is not None:
            pool.shutdown(cancel_futures=True)

    rec = Recorder.merged([r["recorder"] for r in shard_results], t_open)
    outcomes = sorted((o for r in shard_results for o in r["outcomes"]), key=lambda o: o["idx"])
    crashed = sum(r["crashed_count"] for r in shard_results)
    crashed += sum(a["crashed_count"] for r in shard_results for a in r.get("attackers", []))

    attackers: list[dict[str, Any]] = []
    for ai, atk in enumerate(sc.attackers):
        parts = [a for r in shard_results for a in r.get("attackers", []) if a["attacker_index"] == ai]
        outs = sorted((o for p in parts for o in p["outcomes"]), key=lambda o: o["idx"])
        cost = CostAccount.merge([p["cost"] for p in parts])
        attackers.append({"attacker_index": ai, "profile": atk.profile, "config": atk, "outcomes": outs, "cost": cost})

    summary = summarize(
        sc=sc, run_id=run_id, run_index=run_index, run_seed=run_seed, seed_hex=seed_hex, target=driver.kind,
        event_id=event_id, rec=rec, humans=humans, outcomes=outcomes, server=server, invariants=invariants,
        stats=stats, draw_info=draw_info, crashed=crashed, crash_samples=[c for r in shard_results for c in r["crashed"]],
        attackers=attackers, bots_by_attacker=bots_by_attacker,
        environment={**driver.environment(), "entry_only": entry_only, "provisioning": provisioned},
        hooks=draw_info.get("hooks", []),
    )
    _write_artifacts(out_dir / run_id, summary, rec, humans, outcomes, server, attackers, bots_by_attacker)
    summary["artifacts"] = str(out_dir / run_id)
    return summary


async def _coordinate(driver: Driver, sc: Scenario, t_open: float, t_close: float, t_draw: float,
                      shard_futs: list[asyncio.Future], log: Callable[[str], None],
                      progress_cb: Callable[[dict[str, Any]], None] | None, planned_s: float,
                      hooks: list[TimedHook]) -> dict[str, Any]:
    """Drive the event while the load runs: open/close at the planned instants when the target needs
    it (real engine), draw at t_draw, fire timed hooks, report progress. Returns timing info."""
    info: dict[str, Any] = {"drawn": False, "hooks": []}
    opened = closed = False
    pending = sorted(hooks, key=lambda h: h.at_s)
    hook_tasks: list[asyncio.Task] = []
    next_progress = next_stats = time.perf_counter()

    async def fire(h: TimedHook) -> None:
        started = time.perf_counter() - t_open
        rec: dict[str, Any] = {"name": h.name, "planned_at_s": h.at_s, "started_at_s": round(started, 3)}
        try:
            rec["result"] = await h.fn()
            rec["ok"] = True
        except Exception as e:  # reported, never swallowed silently
            rec.update(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-800:])
        rec["finished_at_s"] = round(time.perf_counter() - t_open, 3)
        info["hooks"].append(rec)
        log(f"  hook {h.name}: {'ok' if rec['ok'] else 'FAILED ' + rec['error']} "
            f"({rec['started_at_s']}s -> {rec['finished_at_s']}s)")

    with high_res_timer():
        while not all(f.done() for f in shard_futs):
            now = time.perf_counter()
            if driver.manual_window and not opened and now >= t_open - OPEN_MARGIN_S:
                await _retry(driver.open_window, "open window", log, info)
                opened = True
            if driver.manual_window and not closed and now >= t_close:
                await _retry(driver.close_window, "close window", log, info)
                closed = True
            while pending and now >= t_open + pending[0].at_s:
                hook_tasks.append(asyncio.create_task(fire(pending.pop(0))))
            if not info["drawn"] and now >= t_draw:
                if not closed:
                    await _retry(driver.close_window, "close window", log, info)
                    closed = True
                info["draw"] = await _retry(lambda: driver.draw(sc), "draw", log, info)
                info["drawn"] = True
                info["draw_late_ms"] = round((time.perf_counter() - t_draw) * 1000, 1)
                log(f"  draw done ({info['draw_late_ms']} ms after plan): {info.get('draw')}")
            if now >= next_stats:
                next_stats = now + (1.0 if progress_cb else 5.0)
                try:
                    st = await driver.progress()
                    if now >= next_progress:
                        log(f"  t={now - t_open:6.1f}s phase={str(st.get('phase')):<9} entries={st.get('entries', 0):>7,} "
                            f"states={st.get('states')}")
                        next_progress = now + 5.0
                    if progress_cb:
                        progress_cb({"t_s": round(now - t_open, 2), "phase": st.get("phase"),
                                     "entries": st.get("entries", 0), "states": st.get("states", {}),
                                     "requests": st.get("requests"), "planned_s": planned_s})
                except Exception as e:  # progress is best-effort
                    log(f"  progress unavailable: {e}")
            # wake exactly at the next planned instant if it is near, otherwise every 250 ms
            upcoming = [t for t in (
                None if (not driver.manual_window or opened) else t_open - OPEN_MARGIN_S,
                None if (not driver.manual_window or closed) else t_close,
                None if info["drawn"] else t_draw,
                (t_open + pending[0].at_s) if pending else None) if t is not None]
            until = min(upcoming) - time.perf_counter() if upcoming else 0.25
            await asyncio.sleep(0.25 if until > 0.25 else max(0.0, until))
    if pending:  # the run ended before these were due: say so, do not drop them silently
        for h in pending:
            info["hooks"].append({"name": h.name, "planned_at_s": h.at_s, "ok": False,
                                  "error": "run finished before this hook was due"})
    if hook_tasks:
        await asyncio.gather(*hook_tasks)
    for f in shard_futs:
        if f.exception():
            raise f.exception()  # type: ignore[misc]
    return info


def _write_artifacts(d: Path, summary: dict[str, Any], rec: Recorder, humans, outcomes: list[dict],
                     server: dict[str, Any], attackers: list[dict], bots_by_attacker: dict) -> None:
    import pandas as pd

    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    (d / "recorder.json").write_text(json.dumps(rec.to_dict()), encoding="utf-8")

    # One row per simulated client, with the ground-truth label (simulator-side only) and
    # the server's view joined by user_id. Bots and humans share the columns.
    frames = []
    hu = pd.DataFrame(outcomes)
    if len(hu):
        hu.insert(1, "user_id", [humans.user_ids[i] for i in hu["idx"]])
        hu.insert(2, "label", HUMAN)
        hu.insert(3, "profile", "legit")
        hu.insert(4, "nat_group", humans.nat_group[hu["idx"]])
        hu.insert(5, "client_ip", [humans.client_ips[i] for i in hu["idx"]])
        frames.append(hu)
    for a in attackers:
        bots = bots_by_attacker[a["attacker_index"]]
        bo = pd.DataFrame(a["outcomes"])
        if not len(bo):
            continue
        bo.insert(1, "user_id", [bots.user_ids[i] for i in bo["idx"]])
        bo.insert(2, "label", BOT)
        bo.insert(3, "profile", a["profile"])
        bo.insert(4, "nat_group", -1)
        bo.insert(5, "client_ip", [bots.client_ips[i] for i in bo["idx"]])
        frames.append(bo)
    users = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    merged = users.merge(server["entries"].add_prefix("srv_"), how="left", left_on="user_id", right_on="srv_user_id")
    with gzip.open(d / "users.csv.gz", "wt", encoding="utf-8", newline="") as f:
        merged.drop(columns=["srv_user_id"]).to_csv(f, index=False)
    (d / "decisions.json").write_text(json.dumps(server.get("decisions", [])), encoding="utf-8")
