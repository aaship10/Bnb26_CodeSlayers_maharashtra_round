"""One load-generator shard: a slice of the population on its own event loop.

`run_shard` is a top-level function so it works with multiprocessing's spawn start
method (Windows). Shards rebuild the population and plans from the seed instead of
receiving them, so only a small spec crosses the process boundary.

All shards share one timeline expressed in perf_counter seconds, which is
system-wide on Windows (QPC) and Linux (CLOCK_MONOTONIC).
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import sys
import time
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any, Iterator

from fairdrop_sim.adapters.api_adapter import AdminApi, AuthConfig, Identity, Sender, UserApi, make_session, sleep_until
from fairdrop_sim.bots.base import run_attacker
from fairdrop_sim.challenge.captcha import CaptchaModel
from fairdrop_sim.challenge.solver import Solver
from fairdrop_sim.crowd.human import HumanContext, HumanOutcome, plan_humans, run_human
from fairdrop_sim.crowd.population import build_humans
from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.models.scenario import Scenario

SPAWN_AHEAD_S = 0.25  # create a user's task this long before its first send
TOKEN_BATCH = 5000


@contextlib.contextmanager
def high_res_timer() -> Iterator[None]:
    """Windows timers tick every 15.6 ms, so asyncio.sleep overshoots by ~14 ms and
    every open-loop latency would silently grow by that much. timeBeginPeriod(1)
    brings the overshoot to ~1-2 ms (measured; see README)."""
    if sys.platform != "win32":
        yield
        return
    import ctypes

    winmm = ctypes.WinDLL("winmm")
    winmm.timeBeginPeriod(1)
    try:
        yield
    finally:
        winmm.timeEndPeriod(1)


@dataclass(frozen=True)
class ShardSpec:
    scenario: dict[str, Any]
    run_index: int
    run_seed: int
    run_tag: str
    base_url: str
    event_id: str
    admin_token: str
    sim_key: str | None
    shard: int
    nshards: int
    t_open: float
    t_close: float
    t_draw: float
    t_end: float


def warm_up() -> float:
    """Submitted to each worker before the clock starts, so imports and process spawn
    don't eat into the schedule."""
    return time.perf_counter()


def run_shard(spec: ShardSpec) -> dict[str, Any]:
    with high_res_timer():
        return asyncio.run(shard_main(spec))


async def _mint(base_url: str, admin_token: str, user_ids: list[str]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    admin = AdminApi(base_url, admin_token)
    try:
        for k in range(0, len(user_ids), TOKEN_BATCH):
            tokens.update(await admin.sim_tokens(user_ids[k:k + TOKEN_BATCH]))
    finally:
        await admin.aclose()
    return tokens


async def shard_main(spec: ShardSpec) -> dict[str, Any]:
    sc = Scenario.model_validate(spec.scenario)
    humans = build_humans(sc.legit.count, sc.legit.nat_groups, sc.seed)
    plans = plan_humans(len(humans), sc.event.window_seconds, sc.legit, spec.run_seed)
    mine = list(range(spec.shard, len(humans), spec.nshards))

    auth = AuthConfig(mode=sc.auth_mode, sim_key=spec.sim_key, send_sim_ip=sc.load.sim_client_ip)
    # Bots always control their own IP (that is the point of distributed_botnet/sybil).
    bot_auth = AuthConfig(mode=sc.auth_mode, sim_key=spec.sim_key, send_sim_ip=True)
    tokens: dict[str, str] = {}
    if sc.auth_mode == "jwt_sim_tokens":
        tokens = await _mint(spec.base_url, spec.admin_token, [humans.user_ids[i] for i in mine])

    idents = {
        i: Identity(humans.user_ids[i], humans.device_ids[i], humans.client_ips[i], tokens.get(humans.user_ids[i]))
        for i in mine
    }
    in_flight = max(1, sc.load.max_in_flight // spec.nshards)
    rec = Recorder(spec.t_open)
    dev = sc.legit.device
    human_solver = Solver(hash_rate=math.exp(dev.hash_rate_lognormal_mu), pow_mode=dev.pow_mode,
                          captcha=CaptchaModel(dev.captcha_solve_s_mean, dev.captcha_fail_rate),
                          sim_key=spec.sim_key, event_id=spec.event_id)
    outcomes = {i: HumanOutcome(i) for i in mine}
    async with make_session(spec.base_url, in_flight, sc.load.keepalive_s) as session:
        sender = Sender(session, rec, in_flight, sc.load.request_timeout_s)
        ctx = HumanContext(
            api=UserApi(sender, spec.event_id, auth), cfg=sc.legit, plans=plans, run_seed=spec.run_seed,
            run_tag=spec.run_tag, mode=sc.event.mode, inventory=sc.event.inventory,
            t_open=spec.t_open, t_close=spec.t_close, t_draw=spec.t_draw, t_end=spec.t_end,
            solver=human_solver,
        )

        async def run_humans() -> list[Any]:
            tasks: list[asyncio.Task[None]] = []
            # Spawn each user shortly before its arrival, so only users that are "on the
            # site" hold a task (50k sleeping tasks would cost memory and timer churn).
            for i in sorted(mine, key=lambda j: plans.arrival_s[j]):
                await sleep_until(spec.t_open + float(plans.arrival_s[i]) - SPAWN_AHEAD_S)
                tasks.append(asyncio.create_task(run_human(ctx, i, idents[i], outcomes[i])))
            return await asyncio.gather(*tasks, return_exceptions=True)

        def bot_api_factory() -> UserApi:
            return UserApi(sender, spec.event_id, bot_auth)

        mint = partial(_mint, spec.base_url, spec.admin_token) if sc.auth_mode == "jwt_sim_tokens" else None
        attacker_coros = [
            run_attacker(bot_api_factory, sc, ai, atk, spec.shard, spec.nshards, spec.run_seed, spec.run_tag,
                         spec.t_open, spec.t_close, spec.t_draw, spec.t_end, mint=mint,
                         sim_key=spec.sim_key, event_id=spec.event_id)
            for ai, atk in enumerate(sc.attackers)
        ]
        human_results, *attacker_results = await asyncio.gather(run_humans(), *attacker_coros)

    crashed = [f"{type(r).__name__}: {r}" for r in human_results if isinstance(r, BaseException)]
    return {
        "shard": spec.shard,
        "recorder": rec.to_dict(),
        "outcomes": [asdict(outcomes[i]) for i in mine],
        "attackers": attacker_results,
        "crashed": crashed[:20],
        "crashed_count": len(crashed),
    }
