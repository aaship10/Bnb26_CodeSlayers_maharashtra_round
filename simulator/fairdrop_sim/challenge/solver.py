"""Challenge solver shared by humans and bots. Satisfies crowd.human.ChallengeSolver.

Given a challenge object (from a CHALLENGE_REQUIRED error body), it returns
(challenge_id, solution) or None to give up, and models the TIME the solve takes so
the caller's next send is delayed realistically:

  * PoW  - always solved for real (the server verifies the nonce), so the hashes are
           genuine work and counted. The modelled wall time is hashes / hash_rate.
             - device "real":          no extra sleep (this CPU already spent it).
             - device "modelled_delay": sleep (hashes / hash_rate) so a modelled phone
               or a fast bot takes a realistic time without us owning that hardware.
  * CAPTCHA - modelled (captcha.py): sleep the solve time, then return the mock token
              (or fail with probability fail_rate).

Costs (real PoW hashes, CAPTCHA solves) are reported to an optional CostSink.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Protocol

from fairdrop_sim.challenge import pow as powmod
from fairdrop_sim.challenge.captcha import CaptchaModel, mint_sim_token


class CostSink(Protocol):
    def add_pow_hashes(self, n: int) -> None: ...
    def add_captcha_solves(self, n: int) -> None: ...
    def note_give_up(self, reason: str) -> None: ...


@dataclass
class Solver:
    """One solver per client (hash_rate varies per device/bot)."""

    hash_rate: float = 1e6
    pow_mode: str = "modelled_delay"  # real | modelled_delay
    captcha: CaptchaModel | None = None
    max_pow_bits: int = 28  # refuse absurd difficulty rather than hang a run
    sink: CostSink | None = None
    # When both are set, a CAPTCHA solution is B's SIM_KEY-signed token for this user and event (what the
    # real stack's mock provider accepts in simulation mode); otherwise the fixed demo token.
    sim_key: str | None = None
    event_id: str | None = None

    async def solve(self, challenge: dict, rnd: random.Random, ident: object | None = None) -> tuple[str, str] | None:
        ctype = challenge.get("type")
        cid = challenge.get("id")
        if not cid:
            return None
        if ctype == "pow":
            return await self._pow(cid, challenge.get("pow") or {}, rnd)
        if ctype == "captcha":
            return await self._captcha(cid, rnd, getattr(ident, "user_id", None))
        if self.sink:
            self.sink.note_give_up(f"unknown_challenge:{ctype}")
        return None

    async def _pow(self, cid: str, params: dict, rnd: random.Random) -> tuple[str, str] | None:
        bits = int(params.get("difficulty_bits", 0))
        prefix = params.get("prefix")
        if prefix is None or bits > self.max_pow_bits:
            if self.sink:
                self.sink.note_give_up(f"pow_too_hard:{bits}")
            return None
        sol = powmod.solve(prefix, bits)  # smallest nonce; always succeeds for sane bits
        assert sol is not None
        if self.sink:
            self.sink.add_pow_hashes(sol.hashes)
        if self.pow_mode == "modelled_delay" and self.hash_rate > 0:
            await asyncio.sleep(sol.hashes / self.hash_rate)
        return cid, sol.nonce

    async def _captcha(self, cid: str, rnd: random.Random, user_id: str | None = None) -> tuple[str, str] | None:
        model = self.captcha or CaptchaModel()
        await asyncio.sleep(model.solve_time(rnd))
        if self.sink:
            self.sink.add_captcha_solves(1)
        if not model.succeeds(rnd):
            if self.sink:
                self.sink.note_give_up("captcha_failed")
            return None
        if self.sim_key and self.event_id and user_id:
            return cid, mint_sim_token(self.sim_key, user_id, self.event_id)
        return cid, model.token
