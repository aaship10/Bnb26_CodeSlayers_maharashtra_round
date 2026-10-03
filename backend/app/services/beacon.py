"""Public randomness beacons.

At scheduling we commit to a beacon *round* whose value will only be published
after the entry window closes, so the organiser cannot pick favourable
randomness after seeing the entrants. At draw time we fetch that round's value.

Two providers implement the same interface:
  * MockBeacon  - deterministic, offline; default for dev/tests/demos.
  * DrandBeacon - drand "quicknet" via the public HTTP API.

drand details (checked against the live API on 2026-10-03):
  GET https://api.drand.sh/{chain_hash}/info
      -> {"period": 3, "genesis_time": 1692803367, "hash": ..., "schemeID": "bls-unchained-g1-rfc9380", ...}
  GET https://api.drand.sh/{chain_hash}/public/{round}
      -> {"round": N, "randomness": "<hex>", "signature": "<hex>"}; HTTP 425 if N is in the future
  randomness == SHA-256(signature)  (verified for round 1000)
  Round r is published at genesis_time + (r - 1) * period.
We do NOT verify the BLS signature (that needs a pairing library); we check the
randomness/signature relationship and store the signature so anyone can verify
it against drand's published public key.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from app.config import get_settings


@dataclass(frozen=True)
class BeaconValue:
    round: int
    randomness: str   # lowercase hex
    signature: str    # lowercase hex ("" for the mock)


class BeaconProvider(Protocol):
    name: str

    def round_after(self, t: datetime) -> int:
        """First round whose publication time is strictly after t."""

    def round_time(self, round_: int) -> datetime:
        """Publication time of a round."""

    async def fetch(self, round_: int) -> BeaconValue | None:
        """The round's value, or None if it is not published yet."""


class _PeriodicSchedule:
    genesis: int
    period: int

    def round_time(self, round_: int) -> datetime:
        return datetime.fromtimestamp(self.genesis + (round_ - 1) * self.period, UTC)

    def round_after(self, t: datetime) -> int:
        elapsed = t.timestamp() - self.genesis
        # Round r is published at genesis + (r-1)*period; pick the smallest r with
        # that time > t.
        return max(1, math.floor(elapsed / self.period) + 2)


class MockBeacon(_PeriodicSchedule):
    """Deterministic stand-in with drand's schedule shape.

    randomness(r) = SHA-256("fairdrop-mock-beacon:" || decimal(r)). Always
    "published" (no waiting), so compressed demos and tests never block. It is
    NOT unpredictable; use DrandBeacon for anything public.
    """

    name = "mock"
    genesis = 1692803367
    period = 3

    @staticmethod
    def value_for(round_: int) -> BeaconValue:
        r = hashlib.sha256(f"fairdrop-mock-beacon:{round_}".encode()).hexdigest()
        return BeaconValue(round=round_, randomness=r, signature="")

    async def fetch(self, round_: int) -> BeaconValue | None:
        return self.value_for(round_)


class DrandBeacon(_PeriodicSchedule):
    name = "drand-quicknet"
    QUICKNET = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
    genesis = 1692803367
    period = 3

    def __init__(self, base_url: str = "https://api.drand.sh", chain_hash: str = QUICKNET,
                 timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.chain_hash = chain_hash
        self.timeout = timeout

    async def fetch(self, round_: int) -> BeaconValue | None:
        url = f"{self.base_url}/{self.chain_hash}/public/{round_}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url)
        if resp.status_code in (404, 425):   # 425 Too Early: round not yet produced
            return None
        resp.raise_for_status()
        data = resp.json()
        if int(data["round"]) != round_:
            raise ValueError(f"drand returned round {data['round']}, expected {round_}")
        randomness = data["randomness"].lower()
        signature = data["signature"].lower()
        if hashlib.sha256(bytes.fromhex(signature)).hexdigest() != randomness:
            raise ValueError("drand randomness is not SHA-256(signature)")
        return BeaconValue(round=round_, randomness=randomness, signature=signature)


def get_beacon() -> BeaconProvider:
    return DrandBeacon() if get_settings().beacon_provider == "drand" else MockBeacon()
