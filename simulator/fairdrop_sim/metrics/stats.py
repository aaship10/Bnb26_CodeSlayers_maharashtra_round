"""Statistics helpers. Stage 1 holds only what the synthetic sample generator
needs; Stage 4 adds correlation, permutation tests, Jain and Gini."""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from fairdrop_sim.models.results import Stat

Z95 = 1.959963984540054


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be in [0, n]")
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def proportion_stat(successes: int, n: int) -> Stat:
    lo, hi = wilson(successes, n)
    return Stat(mean=successes / n, ci_low=lo, ci_high=hi, n=n)


def bootstrap_mean_ci(
    values: Sequence[float], resamples: int = 10_000, seed: int = 0, level: float = 0.95
) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean. Deterministic for a given seed."""
    x = np.asarray(values, dtype=float)
    if x.size < 2:
        raise ValueError("need at least 2 values")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, x.size, size=(resamples, x.size))
    means = x[idx].mean(axis=1)
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def mean_stat(values: Sequence[float], resamples: int = 10_000, seed: int = 0) -> Stat:
    """Stat over per-run values (n = number of runs). Needs n >= 2: a Stat always has a CI."""
    x = [float(v) for v in values]
    if len(x) < 2:
        raise ValueError(f"a Stat needs at least 2 values, got {len(x)}; use value_or_stat for optional CIs")
    lo, hi = bootstrap_mean_ci(x, resamples=resamples, seed=seed)
    m = float(np.mean(x))
    # a percentile bootstrap can exclude the mean by float rounding on near-constant data
    return Stat(mean=m, ci_low=min(lo, m), ci_high=max(hi, m), n=len(x))


def value_or_stat(values: Sequence[float], resamples: int = 10_000, seed: int = 0) -> Stat | float:
    """For fields typed Stat | number: a Stat when n >= 2, else the bare value (labelled 'no CI' by the UI)."""
    x = [float(v) for v in values]
    if not x:
        raise ValueError("no values")
    return mean_stat(x, resamples, seed) if len(x) >= 2 else x[0]


# --------------------------------------------------------------------------- correlation

def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman rank correlation. None when undefined (n < 2 or a constant input)."""
    a, b = np.asarray(x, float), np.asarray(y, float)
    if a.size < 2 or np.all(a == a[0]) or np.all(b == b[0]):
        return None
    from scipy.stats import spearmanr

    rho = spearmanr(a, b).statistic
    return None if np.isnan(rho) else float(rho)


def point_biserial(binary: Sequence[int], values: Sequence[float]) -> float | None:
    """Correlation between a 0/1 label and a continuous value. None when degenerate."""
    g = np.asarray(binary, int)
    v = np.asarray(values, float)
    if g.size < 2 or g.min() == g.max() or np.all(v == v[0]):
        return None
    from scipy.stats import pointbiserialr

    r = pointbiserialr(g, v).statistic
    return None if np.isnan(r) else float(r)


def permutation_pvalue(order: Sequence[float], won: Sequence[int], n_perm: int = 10_000,
                       seed: int = 0) -> float | None:
    """Two-sided permutation test of H0: winning is independent of arrival order.

    Statistic = Spearman(order, won). Shuffle the win labels n_perm times; p = share of
    permutations whose |rho| >= |observed|, with the +1/+1 small-sample correction.
    None when the observed correlation is undefined."""
    obs = spearman(order, won)
    if obs is None:
        return None
    o = np.asarray(order, float)
    w = np.asarray(won, int)
    rng = np.random.default_rng(seed)
    # Spearman(order, perm) reduces to a correlation of ranks; rank once, then permute.
    from scipy.stats import rankdata

    ro = rankdata(o)
    ro = ro - ro.mean()
    denom = np.sqrt((ro**2).sum())
    if denom == 0:
        return None
    ge = 0
    abs_obs = abs(obs) - 1e-12
    for _ in range(n_perm):
        perm = rng.permutation(w).astype(float)
        rp = rankdata(perm)
        rp = rp - rp.mean()
        d = np.sqrt((rp**2).sum())
        rho = 0.0 if d == 0 else float((ro * rp).sum() / (denom * d))
        if abs(rho) >= abs_obs:
            ge += 1
    return (ge + 1) / (n_perm + 1)


# --------------------------------------------------------------------------- distribution fairness

def jain_index(x: Sequence[float]) -> float | None:
    """Jain's fairness index (Σx)² / (n·Σx²), in [1/n, 1]. 1 = perfectly equal.
    None for an empty input or all-zero input (no allocations to judge)."""
    a = np.asarray(x, float)
    if a.size == 0 or np.all(a == 0):
        return None
    return float(a.sum() ** 2 / (a.size * (a**2).sum()))


def gini(x: Sequence[float]) -> float | None:
    """Gini coefficient in [0, 1]. 0 = perfectly equal. None for empty/all-zero/negative."""
    a = np.sort(np.asarray(x, float))
    if a.size == 0 or a.min() < 0 or a.sum() == 0:
        return None
    n = a.size
    idx = np.arange(1, n + 1)
    return float((2 * (idx * a).sum() - (n + 1) * a.sum()) / (n * a.sum()))


def fair_lottery_jain(n_identities: int, seats_per_run: int, runs: int) -> float | None:
    """Analytic reference: expected Jain index of win counts under a uniform lottery
    (each run draws `seats_per_run` of `n_identities` without replacement). Lets charts
    show 'a fair lottery would score this', since variance alone keeps Jain < 1."""
    n, k, r = n_identities, seats_per_run, runs
    if n <= 0 or k <= 0 or r <= 0 or k > n:
        return None
    p = k / n
    mean = r * p
    var = r * p * (1 - p)  # per-run draws are independent across runs
    e_x2 = var + mean**2
    return float(mean**2 / e_x2) if e_x2 > 0 else None


# --------------------------------------------------------------------------- classification

def confusion_rates(true_positive: int, false_positive: int, false_negative: int,
                    true_negative: int) -> dict[str, float | None]:
    """precision, recall, false-positive rate. None where the denominator is 0."""
    tp, fp, fn, tn = true_positive, false_positive, false_negative, true_negative
    return {
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "recall": tp / (tp + fn) if (tp + fn) else None,
        "false_positive_rate": fp / (fp + tn) if (fp + tn) else None,
    }


def ratio_stat(numerators: Sequence[float], denominators: Sequence[float], resamples: int = 10_000,
               seed: int = 0) -> Stat | None:
    """Bootstrap CI for a ratio of sums Σnum/Σden over runs (cost per seat). Resamples
    whole runs (num_i, den_i pairs). None when Σden == 0 (e.g. no seats won)."""
    num = np.asarray(numerators, float)
    den = np.asarray(denominators, float)
    if num.size == 0 or den.sum() == 0:
        return None
    point = float(num.sum() / den.sum())
    if num.size < 2:
        return Stat(mean=point, ci_low=point, ci_high=point, n=num.size)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, num.size, size=(resamples, num.size))
    ds = den[idx].sum(axis=1)
    ratios = np.divide(num[idx].sum(axis=1), ds, out=np.full(resamples, np.nan), where=ds > 0)
    ratios = ratios[~np.isnan(ratios)]
    if ratios.size == 0:
        return Stat(mean=point, ci_low=point, ci_high=point, n=num.size)
    lo, hi = float(np.quantile(ratios, 0.025)), float(np.quantile(ratios, 0.975))
    return Stat(mean=point, ci_low=min(lo, point), ci_high=max(hi, point), n=num.size)


def forced_stat(values: Sequence[float], resamples: int = 10_000, seed: int = 0) -> Stat | None:
    """Always a Stat (fairness fields are never bare numbers). Degenerate CI when n < 2.
    None only when there are no values at all."""
    x = [float(v) for v in values if v is not None]
    if not x:
        return None
    if len(x) < 2:
        return Stat(mean=x[0], ci_low=x[0], ci_high=x[0], n=1)
    return mean_stat(x, resamples=resamples, seed=seed)


def bootstrap_stat(x: Sequence[float], fn, resamples: int = 10_000, seed: int = 0) -> Stat | None:
    """Bootstrap CI for an arbitrary statistic fn(array) by resampling the elements of x
    (e.g. Jain/Gini over identities). n = len(x). None when fn is undefined on x."""
    a = np.asarray(x, float)
    point = fn(a)
    if a.size == 0 or point is None:
        return None
    if a.size < 2:
        return Stat(mean=float(point), ci_low=float(point), ci_high=float(point), n=a.size)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(resamples):
        v = fn(a[rng.integers(0, a.size, a.size)])
        if v is not None:
            vals.append(v)
    if not vals:
        return Stat(mean=float(point), ci_low=float(point), ci_high=float(point), n=a.size)
    lo, hi = float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))
    return Stat(mean=float(point), ci_low=min(lo, float(point)), ci_high=max(hi, float(point)), n=a.size)
