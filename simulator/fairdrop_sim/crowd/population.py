"""The simulated population: identities, client IPs, device ids and NAT groups.

The population is derived from the scenario's master seed only (not the run index),
so repeated runs see the SAME people. That is what lets Jain/Gini measure how wins
spread over identities across runs.

Ground truth (`labels`) stays in this process and in our result files. Ids, device
ids and IPs are hashes/addresses that carry no trace of the label.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from fairdrop_sim.models.scenario import NatGroups
from fairdrop_sim.seeds import derive_seed

HUMAN, BOT = "legit", "bot"


def _uuid(seed: int, *parts: object) -> str:
    h = hashlib.sha256(("|".join(map(str, (seed, *parts)))).encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-{'89ab'[int(h[16], 16) % 4]}{h[17:20]}-{h[20:32]}"


def unique_ip(i: int) -> str:
    """Distinct address per index (10.0.0.0/8, up to 16.7M)."""
    return f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}"


def nat_ip(group: int) -> str:
    """Shared campus/CGNAT address (100.64.0.0/10)."""
    return f"100.{64 + ((group >> 16) & 63)}.{(group >> 8) & 255}.{group & 255}"


@dataclass(frozen=True)
class Humans:
    user_ids: list[str]
    device_ids: list[str]
    client_ips: list[str]
    nat_group: np.ndarray  # -1 = own IP

    def __len__(self) -> int:
        return len(self.user_ids)


def build_humans(n: int, nat: NatGroups, master_seed: int) -> Humans:
    pop_seed = derive_seed(master_seed, "population")
    ids = [_uuid(pop_seed, "u", i) for i in range(n)]
    devs = [_uuid(pop_seed, "d", i) for i in range(n)]
    rng = np.random.default_rng(derive_seed(pop_seed, "nat"))
    k = int(round(n * nat.fraction))
    members = rng.permutation(n)[:k]
    group = np.full(n, -1, dtype=np.int64)
    group[members] = np.arange(k) // nat.group_size
    ips = [nat_ip(int(g)) if g >= 0 else unique_ip(i) for i, g in enumerate(group)]
    return Humans(ids, devs, ips, group)


# --------------------------------------------------------------------------- bots

def bot_unique_ip(i: int) -> str:
    """Distinct attacker-controlled address (172.16.0.0/12, ~1M), sent as X-Sim-Client-IP."""
    return f"172.{16 + ((i >> 16) & 15)}.{(i >> 8) & 255}.{i & 255}"


@dataclass(frozen=True)
class Bots:
    """Identities for ONE attacker. user_ids/device_ids are per identity; client_ips maps
    each identity to one of `ips` controlled addresses (M<=N => a Sybil farm shares IPs)."""

    user_ids: list[str]
    device_ids: list[str]
    client_ips: list[str]
    shared_device: bool

    def __len__(self) -> int:
        return len(self.user_ids)


def build_bots(attacker_index: int, n_identities: int, n_ips: int, master_seed: int,
               shared_device: bool = False) -> Bots:
    """Deterministic from the master seed + attacker index, so repeats reuse the same
    fake identities. n_ips distinct addresses are round-robined across the identities."""
    seed = derive_seed(master_seed, "bots", attacker_index)
    uids = [_uuid(seed, "b", i) for i in range(n_identities)]
    n_ips = max(1, min(n_ips, n_identities))
    ip_pool = [bot_unique_ip(derive_seed(seed, "ip", j) % (1 << 20)) for j in range(n_ips)]
    client_ips = [ip_pool[i % n_ips] for i in range(n_identities)]
    if shared_device:  # one device id per IP (tests the signals layer's device grouping)
        devs = [_uuid(seed, "dev", i % n_ips) for i in range(n_identities)]
    else:
        devs = [_uuid(seed, "dev", i) for i in range(n_identities)]
    return Bots(uids, devs, client_ips, shared_device)
