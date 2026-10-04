"""Bot execution: turn an AttackerConfig + BotBehavior into open-loop load.

Each identity runs `run_bot_identity`: an enter phase (once or flood, per profile) then,
if the profile claims, a claim phase (poll+claim, or snipe-flood). Requests go through
the same open-loop Sender as humans, tagged class "bot", so latency and error rates
split legit vs bot. Attacker cost (requests, identities, IPs, real PoW hashes, CAPTCHA
solves) accrues to one CostAccount per attacker per shard.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict, dataclass, field

from fairdrop_sim.adapters.api_adapter import Identity, Resp, UserApi, sleep_until
from fairdrop_sim.bots.cost import CostAccount
from fairdrop_sim.bots.profiles import BotBehavior, behavior_for
from fairdrop_sim.challenge.captcha import CaptchaModel
from fairdrop_sim.challenge.solver import Solver
from fairdrop_sim.crowd.population import build_bots
from fairdrop_sim.models.scenario import AttackerConfig
from fairdrop_sim.seeds import derive_seed

CLS = "bot"
FINAL = {"CLAIMED", "LOST", "EXPIRED"}


@dataclass
class BotContext:
    api: UserApi
    attacker: AttackerConfig
    behavior: BotBehavior
    attacker_index: int
    run_seed: int
    run_tag: str
    mode: str
    t_open: float
    t_close: float
    t_draw: float
    t_end: float
    solver: Solver
    cost: CostAccount


@dataclass(slots=True)
class BotOutcome:
    idx: int
    enter_requests: int = 0
    entered: bool = False
    enter_state: str | None = None
    challenge_seen: int = 0
    status_requests: int = 0
    claim_requests: int = 0
    claimed: bool = False
    seat_no: int | None = None
    last_state: str | None = None
    last_error: str | None = None


def make_solver(a: AttackerConfig, beh: BotBehavior, sink: CostAccount) -> Solver:
    return Solver(
        hash_rate=a.hash_rate,
        pow_mode=a.pow_mode,
        captcha=CaptchaModel(solve_s_mean=a.captcha_solve_s_mean, fail_rate=a.captcha_fail_rate),
        sink=sink,
    )


async def _enter(ctx: BotContext, ident: Identity, out: BotOutcome, t: float,
                 challenge: tuple[str, str] | None = None) -> Resp:
    r = await ctx.api.enter(ident, CLS, t, challenge)
    ctx.cost.note_request(ident.user_id, ident.client_ip)
    out.enter_requests += 1
    if r.ok and not out.entered:
        out.entered = True
        out.enter_state = (r.body or {}).get("state") if isinstance(r.body, dict) else None
    elif r.ok and isinstance(r.body, dict) and out.enter_state is None:
        out.enter_state = r.body.get("state")
    if not r.ok:
        out.last_error = r.code
        if r.code == "CHALLENGE_REQUIRED":
            out.challenge_seen += 1  # counts in both flood and once modes
    return r


def _challenge_of(r: Resp) -> dict | None:
    if isinstance(r.body, dict):
        return (r.body.get("details") or {}).get("challenge")
    return None


def _enter_schedule(ctx: BotContext, rnd: random.Random) -> tuple[float, float, float]:
    """(start, interval, phase_end) in perf_counter seconds for the enter phase."""
    beh, a = ctx.behavior, ctx.attacker
    interval = 1.0 / max(a.effective_rps, 1e-6)
    off = a.start_offset_s
    jit = lambda: rnd.uniform(0, a.timing_jitter_ms / 1000) if a.timing_jitter_ms else 0.0  # noqa: E731
    if beh.enter_window == "zero":
        return ctx.t_open + off, interval, ctx.t_close
    if beh.enter_window == "edge":
        start = ctx.t_close - 0.1 - rnd.uniform(0, 0.05)
        return start, interval, ctx.t_close + 0.5
    if beh.enter_window == "after_close":
        return ctx.t_close + off + jit(), interval, ctx.t_end
    # "window": spread across the open window
    start = ctx.t_open + off + (jit() if beh.enter_mode == "once" else 0.0)
    return start, interval, ctx.t_close


async def _enter_once(ctx: BotContext, ident: Identity, out: BotOutcome, rnd: random.Random) -> None:
    beh = ctx.behavior
    start, interval, phase_end = _enter_schedule(ctx, rnd)
    t = start
    challenge: tuple[str, str] | None = None
    for _ in range(beh.budget):
        if t > phase_end + 1.0:
            break
        r = await _enter(ctx, ident, out, t, challenge)
        challenge = None
        if r.ok:
            if not beh.claim_snipe:  # snipers keep going; a plain bot stops once in
                return
            return
        if r.code == "CHALLENGE_REQUIRED":
            ch = _challenge_of(r)
            solved = None
            if ch and ((ch.get("type") == "pow" and beh.solves_pow) or
                       (ch.get("type") == "captcha" and beh.solves_captcha)):
                solved = await ctx.solver.solve(ch, rnd)
            if solved is None:
                ctx.cost.note_give_up("enter_challenge")
                return
            challenge = solved
            t = time.perf_counter()
            continue
        if r.code == "RATE_LIMITED" and beh.obey_retry_after:
            t = r.t_recv + (r.retry_after_s or 1.0) + rnd.uniform(0, 0.3)
            continue
        if r.code in ("WINDOW_CLOSED", "WINDOW_NOT_OPEN"):
            t += interval  # keep trying at rate in case timing is just off
            continue
        if r.code in ("REJECTED", "FORBIDDEN", "UNAUTHENTICATED"):
            ctx.cost.note_give_up(r.code)
            return
        t += interval


async def _enter_flood(ctx: BotContext, ident: Identity, out: BotOutcome, rnd: random.Random) -> None:
    """Open-loop flood: schedule up to budget enters at the identity's rate; the Sender
    times and throttles them. Does not solve challenges - a flood is about volume."""
    start, interval, phase_end = _enter_schedule(ctx, rnd)
    span = max(0.0, phase_end - start)
    n = min(ctx.behavior.budget, max(1, int(span / interval) + 1))
    times = [start + k * interval for k in range(n)]
    times = [t for t in times if t <= phase_end + 0.001]
    if not times:
        times = [start]
    await asyncio.gather(*(_enter(ctx, ident, out, t) for t in times))


async def _claim(ctx: BotContext, ident: Identity, out: BotOutcome, t: float, key: str,
                 rnd: random.Random) -> Resp:
    r = await ctx.api.claim(ident, CLS, t, key)
    ctx.cost.note_request(ident.user_id, ident.client_ip)
    out.claim_requests += 1
    if r.ok:
        out.claimed = True
        out.last_state = "CLAIMED"
        out.seat_no = (r.body or {}).get("seat_no") if isinstance(r.body, dict) else None
    else:
        out.last_error = r.code
    return r


async def _claim_phase(ctx: BotContext, ident: Identity, out: BotOutcome, rnd: random.Random) -> None:
    """Poll status, claim the instant we are WON. Prompt, no human delay."""
    key = f"bclaim-{ctx.run_tag}-{ident.user_id}"
    interval = 1.0
    t = (ctx.t_draw if ctx.mode == "LOTTERY" else time.perf_counter()) + rnd.uniform(0, 0.2)
    if ctx.mode == "FCFS" and out.enter_state == "WON":
        await _claim(ctx, ident, out, t, key, rnd)
        return
    while t <= ctx.t_end:
        r = await ctx.api.status(ident, CLS, t)
        ctx.cost.note_request(ident.user_id, ident.client_ip)
        out.status_requests += 1
        state = (r.body or {}).get("state") if (r.ok and isinstance(r.body, dict)) else None
        out.last_state = state or out.last_state
        if state == "WON":
            cr = await _claim(ctx, ident, out, r.t_recv, key, rnd)
            if cr.ok or cr.code == "ALREADY_CLAIMED":
                return
        elif state in FINAL:
            return
        t = r.t_recv + interval


async def _claim_snipe(ctx: BotContext, ident: Identity, out: BotOutcome, rnd: random.Random) -> None:
    """Flood claim (with interleaved status) across the whole claim phase, one idem key,
    hoping to grab a seat the instant a human hold expires and it is promoted."""
    key = f"bclaim-{ctx.run_tag}-{ident.user_id}"
    rps = max(ctx.attacker.effective_rps, 1.0)
    interval = 1.0 / rps
    start = ctx.t_draw + rnd.uniform(0, interval)
    n = min(2000, max(1, int((ctx.t_end - start) / interval)))

    async def one(t: float, do_status: bool) -> None:
        if do_status:
            await ctx.api.status(ident, CLS, t)
            ctx.cost.note_request(ident.user_id, ident.client_ip)
            out.status_requests += 1
        await _claim(ctx, ident, out, t, key, rnd)

    await asyncio.gather(*(one(start + k * interval, k % 5 == 0) for k in range(n)))


async def run_bot_identity(ctx: BotContext, idx: int, ident: Identity, out: BotOutcome) -> None:
    rnd = random.Random(derive_seed(ctx.run_seed, "bot", ctx.attacker_index, idx))
    if ctx.behavior.enter_mode == "flood":
        await _enter_flood(ctx, ident, out, rnd)
    else:
        await _enter_once(ctx, ident, out, rnd)
    if ctx.behavior.claim_snipe:
        await _claim_snipe(ctx, ident, out, rnd)
    elif ctx.behavior.claim and out.entered:
        await _claim_phase(ctx, ident, out, rnd)


async def run_attacker(sender_api_factory, sc, attacker_index: int, attacker: AttackerConfig,
                       shard: int, nshards: int, run_seed: int, run_tag: str,
                       t_open: float, t_close: float, t_draw: float, t_end: float, mint=None) -> dict:
    """Run one attacker's identities owned by this shard. `sender_api_factory()` returns a
    UserApi bound to the event. `mint(user_ids)` (jwt mode only) returns {user_id: token}."""
    beh = behavior_for(attacker.profile, {
        "solves_pow": attacker.solves_pow, "solves_captcha": attacker.solves_captcha,
        "obey_retry_after": attacker.obey_retry_after, "shared_device": attacker.shared_device,
    })
    shared_device = beh.shared_device if attacker.shared_device is None else attacker.shared_device
    bots = build_bots(attacker_index, attacker.identities, attacker.ips, sc.seed, shared_device)
    mine = list(range(shard, len(bots), nshards))
    cost = CostAccount()
    ctx = BotContext(
        api=sender_api_factory(), attacker=attacker, behavior=beh, attacker_index=attacker_index,
        run_seed=run_seed, run_tag=run_tag, mode=sc.event.mode, t_open=t_open, t_close=t_close,
        t_draw=t_draw, t_end=t_end, solver=Solver(), cost=cost,
    )
    ctx.solver = make_solver(attacker, beh, cost)
    tokens = await mint([bots.user_ids[i] for i in mine]) if mint else {}
    outcomes = {i: BotOutcome(i) for i in mine}
    idents = {i: Identity(bots.user_ids[i], bots.device_ids[i], bots.client_ips[i],
                          tokens.get(bots.user_ids[i])) for i in mine}

    tasks: list[asyncio.Task] = []
    for i in mine:
        tasks.append(asyncio.create_task(run_bot_identity(ctx, i, idents[i], outcomes[i])))
    # Bots are relatively few vs humans; spawn all up front (their first send waits for its
    # scheduled time inside the Sender). No pre-spawn dispatcher needed.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    crashed = [f"{type(r).__name__}: {r}" for r in results if isinstance(r, BaseException)]
    return {
        "attacker_index": attacker_index,
        "profile": attacker.profile,
        "outcomes": [asdict(outcomes[i]) for i in mine],
        "cost": cost.to_dict(),
        "crashed": crashed[:10],
        "crashed_count": len(crashed),
    }
