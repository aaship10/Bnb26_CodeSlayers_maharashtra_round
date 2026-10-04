"""Attacker cost accounting (section 7 of the brief, METRICS.md §2).

Counts the real, countable things an attacker spends - requests, identities, IPs, PoW
hashes, CAPTCHA solves - and converts them to a MODELLED dollar figure via the
scenario's AttackerCost. The dollar figure is a model (we never buy accounts or pay a
solving service); the counts are real. Reported per attacker and per bot-won seat.

Serializable and additive so shards merge exactly.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

from fairdrop_sim.models.scenario import AttackerCost


@dataclass
class CostAccount:
    requests: int = 0
    pow_hashes: int = 0
    captcha_solves: int = 0
    accounts: set[str] = field(default_factory=set)  # identities that sent at least one request
    ips: set[str] = field(default_factory=set)
    give_ups: Counter = field(default_factory=Counter)

    # CostSink protocol (written to by the solver)
    def add_pow_hashes(self, n: int) -> None:
        self.pow_hashes += n

    def add_captcha_solves(self, n: int) -> None:
        self.captcha_solves += n

    def note_give_up(self, reason: str) -> None:
        self.give_ups[reason] += 1

    def note_request(self, user_id: str, ip: str | None) -> None:
        self.requests += 1
        self.accounts.add(user_id)
        if ip:
            self.ips.add(ip)

    def to_dict(self) -> dict:
        return {
            "requests": self.requests,
            "pow_hashes": self.pow_hashes,
            "captcha_solves": self.captcha_solves,
            "accounts": sorted(self.accounts),
            "ips": sorted(self.ips),
            "give_ups": dict(self.give_ups),
        }

    @classmethod
    def merge(cls, parts: list[dict]) -> "CostAccount":
        acc = cls()
        for p in parts:
            acc.requests += p["requests"]
            acc.pow_hashes += p["pow_hashes"]
            acc.captcha_solves += p["captcha_solves"]
            acc.accounts.update(p["accounts"])
            acc.ips.update(p["ips"])
            acc.give_ups.update(p["give_ups"])
        return acc

    def usd(self, cost: AttackerCost) -> float:
        return round(
            len(self.accounts) * cost.per_account
            + len(self.ips) * cost.per_ip
            + self.captcha_solves * cost.per_captcha
            + self.pow_hashes / 1e9 * cost.per_1e9_hashes
            + self.requests / 1e6 * cost.per_1e6_requests,
            4,
        )

    def summary(self, cost: AttackerCost, seats_won: int) -> dict:
        n = max(seats_won, 0)
        per = (lambda v: round(v / n, 4)) if n else (lambda v: None)
        return {
            "totals": {
                "requests": self.requests,
                "accounts": len(self.accounts),
                "ips": len(self.ips),
                "pow_hashes": self.pow_hashes,
                "captcha_solves": self.captcha_solves,
                "usd_modelled": self.usd(cost),
            },
            "seats_won": seats_won,
            "per_seat": {
                "requests": per(self.requests),
                "accounts": per(len(self.accounts)),
                "pow_hashes": per(self.pow_hashes),
                "captcha_solves": per(self.captcha_solves),
                "usd_modelled": per(self.usd(cost)),
            },
            "give_ups": dict(self.give_ups),
        }
