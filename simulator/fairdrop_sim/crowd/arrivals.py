"""Arrival-time models: when each simulated user first tries to enter (seconds after
the window opens). Vectorised and deterministic for a given numpy Generator."""
from __future__ import annotations

import numpy as np

from fairdrop_sim.models.scenario import ArrivalModel


def arrival_offsets(n: int, window_s: float, model: ArrivalModel, rng: np.random.Generator) -> np.ndarray:
    """spike_tail: round(n * spike_fraction) users arrive in [0, spike_window_fraction * W)
    following a truncated exponential (time constant spike_tau_fraction * W): the
    'everyone hits refresh at 10:00' rush. The rest are uniform on [0, W).
    uniform: everyone uniform on [0, W). Users are shuffled, so index says nothing about time."""
    if n == 0:
        return np.zeros(0)
    if model.kind == "uniform":
        return rng.uniform(0.0, window_s, n)
    k = int(round(n * model.spike_fraction))
    span = model.spike_window_fraction * window_s
    tau = model.spike_tau_fraction * window_s
    u = rng.uniform(0.0, 1.0, k)
    spike = -tau * np.log1p(-u * (1.0 - np.exp(-span / tau)))  # inverse CDF of Exp(tau) truncated to [0, span)
    tail = rng.uniform(0.0, window_s, n - k)
    out = np.concatenate([spike, tail])
    rng.shuffle(out)
    return np.minimum(out, np.nextafter(window_s, 0))
