"""Legitimate-user behaviour.

Timing model (documented in METRICS.md section 3):
  * ACROSS users the load is open-loop: each user's first attempt is scheduled at its
    planned arrival time no matter how the server is doing.
  * WITHIN a user, actions are sequential like a real person: the next action is planned
    from the previous response (response time + think/backoff time). A person does not
    re-send before the page answered, so their own waiting is not counted as latency.
  * Every send time also includes a modelled one-way network delay (device -> edge).

What a human does:
  1. enter at the planned arrival; on 429 wait Retry-After (+jitter), on 5xx/timeouts back
     off exponentially, on CHALLENGE_REQUIRED hand the challenge to the solver (stage 3),
     give up on WINDOW_CLOSED / REJECTED / after MAX_ENTER_ERRORS;
  2. a `retry_fraction` of humans refresh 1..3 more times during the window (idempotent);
  3. after the draw, check /status (see PollModel), claim if WON after a lognormal delay
     unless a no-show; waitlisted humans keep checking only if near the front.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import numpy as np

from fairdrop_sim.crowd.arrivals import arrival_offsets
from fairdrop_sim.models.scenario import LegitConfig
from fairdrop_sim.seeds import derive_seed

if TYPE_CHECKING:
    from fairdrop_sim.adapters.api_adapter import Identity, Resp, UserApi

CLS = "legit"
MAX_ENTER_ERRORS = 6
MAX_CLAIM_ERRORS = 4
FINAL_STATES = {"CLAIMED", "LOST", "EXPIRED"}


class ChallengeSolver(Protocol):
    async def solve(self, challenge: dict, rnd: random.Random) -> tuple[str, str] | None:
        """Return (challenge_id, solution) or None to give up."""


class NoSolver:
    """Stage 2 placeholder: challenges are not solved yet (Stage 3 adds PoW + CAPTCHA)."""

    async def solve(self, challenge: dict, rnd: random.Random) -> tuple[str, str] | None:
        return None


@dataclass
class HumanPlans:
    """Per-run random plan for every human (vectorised; index = population index)."""

    arrival_s: np.ndarray
    refreshes: np.ndarray
    claim_delay_s: np.ndarray
    no_show: np.ndarray


def plan_humans(n: int, window_s: float, cfg: LegitConfig, run_seed: int) -> HumanPlans:
    rng = np.random.default_rng(derive_seed(run_seed, "human-plans"))
    arrival = arrival_offsets(n, window_s, cfg.arrival, rng)
    r = cfg.retry
    retries = rng.integers(r.min_retries, r.max_retries + 1, n)
    refreshes = np.where(rng.uniform(0, 1, n) < r.retry_fraction, retries, 0)
    claim_delay = rng.lognormal(cfg.claim.delay_lognormal_mu, cfg.claim.delay_lognormal_sigma, n)
    no_show = rng.uniform(0, 1, n) < cfg.claim.no_show_rate
    return HumanPlans(arrival, refreshes, claim_delay, no_show)


@dataclass(slots=True)
class HumanOutcome:
    idx: int
    entered: bool = False
    entered_at_s: float | None = None  # client receive time of the first accepted enter, s after open
    enter_requests: int = 0
    enter_last_error: str | None = None
    enter_state: str | None = None  # state in the enter response (FCFS: WON/WAITLISTED)
    status_requests: int = 0
    last_state: str | None = None
    last_waitlist_pos: int | None = None
    claim_requests: int = 0
    claimed: bool = False
    seat_no: int | None = None
    claim_last_error: str | None = None
    gave_up: str | None = None


@dataclass
class HumanContext:
    """Everything a shard's human coroutines share."""

    api: UserApi
    cfg: LegitConfig
    plans: HumanPlans
    run_seed: int
    run_tag: str
    mode: str  # LOTTERY | FCFS
    inventory: int
    t_open: float  # perf_counter
    t_close: float
    t_draw: float
    t_end: float
    solver: ChallengeSolver = field(default_factory=NoSolver)


def _net(cfg: LegitConfig, rnd: random.Random) -> float:
    return rnd.lognormvariate(cfg.network.latency_lognormal_mu, cfg.network.latency_lognormal_sigma) / 1000


def _transient(r: Resp) -> bool:
    return r.status == 0 or r.status >= 500


async def run_human(ctx: HumanContext, i: int, ident: Identity, out: HumanOutcome) -> None:
    rnd = random.Random(derive_seed(ctx.run_seed, "human", i))
    t = ctx.t_open + float(ctx.plans.arrival_s[i]) + _net(ctx.cfg, rnd)
    last = await _enter_flow(ctx, ident, out, rnd, t)

    # Refreshes during the window (idempotent re-entries; also a second chance after a lockout).
    for _ in range(int(ctx.plans.refreshes[i])):
        t = max(last, time.perf_counter()) + rnd.expovariate(1000 / ctx.cfg.retry.jitter_ms_mean) + _net(ctx.cfg, rnd)
        if t >= ctx.t_close:
            break
        last = await _enter_flow(ctx, ident, out, rnd, t)

    if not out.entered:
        return
    if ctx.mode == "FCFS" and out.enter_state == "WON":
        await _claim_flow(ctx, ident, out, rnd, last + float(ctx.plans.claim_delay_s[i]), i)
        return
    start = ctx.t_draw if ctx.mode == "LOTTERY" else last
    await _poll_flow(ctx, ident, out, rnd, max(start, last), i)


async def _enter_flow(ctx: HumanContext, ident: Identity, out: HumanOutcome, rnd: random.Random, t: float) -> float:
    """Returns the perf time of the last response."""
    errors = 0
    challenge: tuple[str, str] | None = None
    while True:
        r = await ctx.api.enter(ident, CLS, t, challenge)
        out.enter_requests += 1
        challenge = None
        if r.ok:
            if not out.entered:
                out.entered = True
                out.entered_at_s = r.t_recv - ctx.t_open
                out.enter_state = (r.body or {}).get("state") if isinstance(r.body, dict) else None
            return r.t_recv
        out.enter_last_error = r.code
        if r.code in ("WINDOW_CLOSED", "REJECTED", "UNAUTHENTICATED", "FORBIDDEN", "VALIDATION_ERROR"):
            out.gave_up = out.gave_up or r.code
            return r.t_recv
        errors += 1
        if errors > MAX_ENTER_ERRORS:
            out.gave_up = out.gave_up or f"too_many_errors:{r.code}"
            return r.t_recv
        if r.code == "CHALLENGE_REQUIRED":
            ch = ((r.body or {}).get("details") or {}).get("challenge") if isinstance(r.body, dict) else None
            challenge = await ctx.solver.solve(ch, rnd) if ch else None
            if challenge is None:
                out.gave_up = out.gave_up or "challenge_unsolved"
                return r.t_recv
            wait = 0.0  # solver already spent the (real or modelled) solving time
            base = time.perf_counter()
        elif r.code == "RATE_LIMITED":
            wait = (r.retry_after_s or 1.0) + rnd.uniform(0, 0.5)
            base = r.t_recv
        elif r.code == "WINDOW_NOT_OPEN":
            wait = 0.5 + rnd.random()
            base = r.t_recv
        elif _transient(r):
            wait = min(8.0, 0.5 * 2**errors) * rnd.uniform(0.5, 1.5)
            base = r.t_recv
        else:
            out.gave_up = out.gave_up or (r.code or "unknown")
            return r.t_recv
        t = base + wait + _net(ctx.cfg, rnd)
        if t > ctx.t_close + 1.0:
            out.gave_up = out.gave_up or "window_over"
            return r.t_recv


async def _poll_flow(ctx: HumanContext, ident: Identity, out: HumanOutcome, rnd: random.Random,
                     start: float, i: int) -> None:
    poll = ctx.cfg.poll
    t = start + rnd.expovariate(1 / poll.interval_s_mean) + _net(ctx.cfg, rnd)
    for _ in range(poll.max_polls):
        if t > ctx.t_end:
            return
        r = await ctx.api.status(ident, CLS, t)
        out.status_requests += 1
        if not r.ok:
            t = r.t_recv + (r.retry_after_s or rnd.expovariate(1 / poll.interval_s_mean)) + _net(ctx.cfg, rnd)
            continue
        body = r.body if isinstance(r.body, dict) else {}
        state = body.get("state")
        out.last_state = state
        if state == "WON":
            if not ctx.plans.no_show[i]:
                await _claim_flow(ctx, ident, out, rnd, r.t_recv + float(ctx.plans.claim_delay_s[i]), i)
            else:
                out.gave_up = "no_show"
            return
        if state in FINAL_STATES:
            return
        if state == "WAITLISTED":
            pos = body.get("waitlist_position")
            out.last_waitlist_pos = pos
            if isinstance(pos, int) and pos > poll.waitlist_attention * ctx.inventory:
                out.gave_up = "waitlist_too_far"
                return
        t = r.t_recv + rnd.expovariate(1 / poll.interval_s_mean) + _net(ctx.cfg, rnd)


async def _claim_flow(ctx: HumanContext, ident: Identity, out: HumanOutcome, rnd: random.Random,
                      t: float, i: int) -> None:
    key = f"claim-{ctx.run_tag}-{ident.user_id}"  # one key per user per run; retries reuse it
    errors = 0
    challenge: tuple[str, str] | None = None
    while t <= ctx.t_end:
        r = await ctx.api.claim(ident, CLS, t, key, challenge)
        out.claim_requests += 1
        challenge = None
        if r.ok:
            out.claimed = True
            out.seat_no = (r.body or {}).get("seat_no") if isinstance(r.body, dict) else None
            out.last_state = "CLAIMED"
            return
        out.claim_last_error = r.code
        errors += 1
        if errors > MAX_CLAIM_ERRORS:
            return
        if r.code == "RATE_LIMITED":
            t = r.t_recv + (r.retry_after_s or 1.0) + rnd.uniform(0, 0.5)
        elif r.code == "CHALLENGE_REQUIRED":
            ch = ((r.body or {}).get("details") or {}).get("challenge") if isinstance(r.body, dict) else None
            challenge = await ctx.solver.solve(ch, rnd) if ch else None
            if challenge is None:
                out.gave_up = "claim_challenge_unsolved"
                return
            t = time.perf_counter()
        elif _transient(r):
            t = r.t_recv + min(4.0, 0.25 * 2**errors) * rnd.uniform(0.5, 1.5)
        else:  # NOT_WINNER, HOLD_EXPIRED, ALREADY_CLAIMED ...
            if r.code == "HOLD_EXPIRED":
                out.last_state = "EXPIRED"
            return
