"""Execute ONE run of a scenario against a target and write its artifacts.

Timeline (perf_counter seconds; t_open = window opens):
    now ........ t_open = now + lead_s ......... t_close = t_open + W
                 t_draw = t_close + draw_delay_s  (coordinator closes if needed, then draws)
                 t_end  = t_draw + claim_phase_s  (users stop sending new requests)

The server is told to open at t_open - CLOCK_MARGIN_S with a window CLOCK_MARGIN_S
longer at each end, so the 15.6 ms Windows wall-clock granularity can't make an
on-time first or last arrival bounce off WINDOW_NOT_OPEN / WINDOW_CLOSED.
Stage 5 wraps this in repeats, ablations and result aggregation.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fairdrop_sim.adapters.api_adapter import AdminApi
from fairdrop_sim.adapters.db_adapter import load_server_view
from fairdrop_sim.adapters.provisioning import provision
from fairdrop_sim.bots.cost import CostAccount
from fairdrop_sim.crowd.population import BOT, HUMAN, build_bots, build_humans
from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.engine.shard import ShardSpec, high_res_timer, run_shard, shard_main, warm_up
from fairdrop_sim.engine.summary import summarize
from fairdrop_sim.models.scenario import Scenario
from fairdrop_sim.seeds import derive_seed, derive_seed_hex

CLOCK_MARGIN_S = 0.05
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "results" / "runs"


class TargetMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class TargetConfig:
    base_url: str = "http://127.0.0.1:8200"
    admin_token: str = "dev-admin-token"
    sim_key: str | None = "dev-sim-key"


def default_event_id(sc: Scenario) -> str:
    e = sc.event
    h = hashlib.sha256(f"{e.inventory}|{e.mode}|{e.window_seconds}|{e.claim_ttl_seconds}".encode()).hexdigest()[:6]
    return sc.event_id or f"evt_sim_{sc.name}_{h}"[:64]


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _prepare_event(admin: AdminApi, sc: Scenario, event_id: str, seed_hex: str) -> None:
    defences = sc.event.defences.model_dump(exclude_none=True)
    if await admin.get_event(event_id) is None:
        await admin.create_event({
            "id": event_id, "name": f"SIM {sc.name}", "inventory": sc.event.inventory, "mode": sc.event.mode,
            "window_seconds": sc.event.window_seconds, "claim_ttl_seconds": sc.event.claim_ttl_seconds,
            "server_seed_hex": seed_hex, "config": {"defences": defences},
        })
    else:
        await admin.reset(event_id, seed_hex)
        await admin.set_defences(event_id, defences)


async def _detect_target(admin: AdminApi, sc: Scenario) -> str:
    health = await admin.health()
    actual = "mock" if health.get("mock") is True else "real"
    if actual != sc.target:
        raise TargetMismatch(f"scenario says target={sc.target!r} but the server at {admin.c.base_url} is {actual!r}")
    return actual


async def execute_run(sc: Scenario, run_index: int, target: TargetConfig, out_dir: Path = DEFAULT_OUT,
                      log: Callable[[str], None] = print,
                      progress_cb: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """`progress_cb` (optional) is called about once a second with
    {t_s, phase, entries, states, requests?, planned_s}: t_s is seconds since the window
    opened (negative during the lead-in), planned_s the planned run length after opening."""
    run_seed = derive_seed(sc.seed, "run", run_index)
    seed_hex = derive_seed_hex(sc.seed, "run", run_index)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{sc.name}-r{run_index:02d}-{stamp}"
    event_id = default_event_id(sc)
    procs = sc.load.procs
    W, ttl = sc.event.window_seconds, sc.event.claim_ttl_seconds
    claim_phase = sc.load.claim_phase_s or 3 * ttl

    admin = AdminApi(target.base_url, target.admin_token)
    pool: ProcessPoolExecutor | None = None
    shard_futs: list[asyncio.Future] = []
    try:
        actual = await _detect_target(admin, sc)
        humans = build_humans(sc.legit.count, sc.legit.nat_groups, sc.seed)
        await provision(actual, humans, sc.legit.register_sample)
        await _prepare_event(admin, sc, event_id, seed_hex)

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
        await admin.schedule(event_id, _iso(wall_now + sc.load.lead_s - CLOCK_MARGIN_S), W + 2 * CLOCK_MARGIN_S)
        log(f"[{run_id}] event {event_id} opens in {sc.load.lead_s:.1f}s; window {W:.0f}s, "
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

        draw_info = await _coordinate(admin, sc, event_id, t_close, t_draw, shard_futs, log, t_open,
                                      progress_cb, t_end - t_open)
        shard_results = await asyncio.gather(*shard_futs)

        invariants = await admin.invariants(event_id)
        server = await load_server_view(actual, admin, event_id)
        stats = await admin.stats(event_id)
    finally:
        for f in shard_futs:  # a cancelled/failed run must not leave load running against the target
            if not f.done():
                f.cancel()
        await admin.aclose()
        if pool is not None:
            pool.shutdown(cancel_futures=True)

    rec = Recorder.merged([r["recorder"] for r in shard_results], t_open)
    outcomes = sorted((o for r in shard_results for o in r["outcomes"]), key=lambda o: o["idx"])
    crashed = sum(r["crashed_count"] for r in shard_results)
    crashed += sum(a["crashed_count"] for r in shard_results for a in r.get("attackers", []))

    attackers: list[dict[str, Any]] = []
    bots_by_attacker: dict[int, Any] = {}
    for ai, atk in enumerate(sc.attackers):
        parts = [a for r in shard_results for a in r.get("attackers", []) if a["attacker_index"] == ai]
        outs = sorted((o for p in parts for o in p["outcomes"]), key=lambda o: o["idx"])
        cost = CostAccount.merge([p["cost"] for p in parts])
        bots = build_bots(ai, atk.identities, atk.ips, sc.seed,
                          atk.shared_device if atk.shared_device is not None else False)
        bots_by_attacker[ai] = bots
        attackers.append({"attacker_index": ai, "profile": atk.profile, "config": atk, "outcomes": outs, "cost": cost})

    summary = summarize(
        sc=sc, run_id=run_id, run_index=run_index, run_seed=run_seed, seed_hex=seed_hex, target=actual,
        event_id=event_id, rec=rec, humans=humans, outcomes=outcomes, server=server, invariants=invariants,
        stats=stats, draw_info=draw_info, crashed=crashed, crash_samples=[c for r in shard_results for c in r["crashed"]],
        attackers=attackers, bots_by_attacker=bots_by_attacker,
    )
    _write_artifacts(out_dir / run_id, summary, rec, humans, outcomes, server, attackers, bots_by_attacker)
    summary["artifacts"] = str(out_dir / run_id)
    return summary


async def _coordinate(admin: AdminApi, sc: Scenario, event_id: str, t_close: float, t_draw: float,
                      shard_futs: list[asyncio.Future], log: Callable[[str], None], t_open: float,
                      progress_cb: Callable[[dict[str, Any]], None] | None = None,
                      planned_s: float = 0.0) -> dict[str, Any]:
    """Progress lines every few seconds; close + draw at t_draw. Returns draw timing info."""
    info: dict[str, Any] = {"drawn": False}
    next_progress = next_stats = time.perf_counter()
    with high_res_timer():
        while not all(f.done() for f in shard_futs):
            now = time.perf_counter()
            if not info["drawn"] and now >= t_draw:
                ev = await admin.get_event(event_id) or {}
                if ev.get("phase") == "OPEN":
                    await admin.close(event_id)
                if sc.event.mode == "LOTTERY":
                    d = await admin.draw(event_id)
                    info["draw"] = d.get("draw")
                info["drawn"] = True
                info["draw_late_ms"] = round((time.perf_counter() - t_draw) * 1000, 1)
                log(f"  draw done ({info['draw_late_ms']} ms after plan): {info.get('draw')}")
            if now >= next_stats:
                next_stats = now + (1.0 if progress_cb else 5.0)
                try:
                    st = await admin.stats(event_id)
                    if now >= next_progress:
                        log(f"  t={now - t_open:6.1f}s phase={st.get('phase'):<9} entries={st.get('entries', 0):>7,} "
                            f"states={st.get('states')}")
                        next_progress = now + 5.0
                    if progress_cb:
                        reqs = st.get("requests")  # mock only; A's stats may not carry request counters
                        progress_cb({
                            "t_s": round(now - t_open, 2), "phase": st.get("phase"),
                            "entries": st.get("entries", 0), "states": st.get("states", {}),
                            "requests": sum(v for k, v in reqs.items() if k in ("enter", "status", "claim"))
                            if isinstance(reqs, dict) else None,
                            "planned_s": planned_s,
                        })
                except Exception as e:  # progress is best-effort
                    log(f"  progress unavailable: {e}")
            # wake exactly at the draw time if it is near, otherwise every 250 ms
            until_draw = t_draw - time.perf_counter()
            await asyncio.sleep(0.25 if info["drawn"] or until_draw > 0.25 else max(0.0, until_draw))
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
