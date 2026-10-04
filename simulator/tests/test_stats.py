from __future__ import annotations

import pytest

from fairdrop_sim.metrics.stats import bootstrap_mean_ci, mean_stat, proportion_stat, value_or_stat, wilson


def test_wilson_known_values():
    # reference: statsmodels proportion_confint(20, 100, method="wilson")
    lo, hi = wilson(20, 100)
    assert lo == pytest.approx(0.13332, abs=1e-4) and hi == pytest.approx(0.28883, abs=1e-4)
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.27753, abs=1e-4)


def test_proportion_stat():
    s = proportion_stat(50, 100)
    assert s.mean == 0.5 and s.n == 100 and s.ci_low < 0.5 < s.ci_high


def test_bootstrap_deterministic_and_contains_mean():
    vals = [0.1, 0.2, 0.15, 0.3, 0.25, 0.18]
    a = bootstrap_mean_ci(vals, seed=3)
    assert a == bootstrap_mean_ci(vals, seed=3)
    assert a[0] < sum(vals) / len(vals) < a[1]


def test_single_value_is_a_bare_number_never_a_ci_less_stat():
    with pytest.raises(ValueError):
        mean_stat([0.4])
    assert value_or_stat([0.4]) == 0.4
    s = value_or_stat([0.4, 0.5])
    assert s.n == 2 and s.ci_low <= s.mean <= s.ci_high


def test_constant_values_keep_mean_inside_ci():
    s = mean_stat([0.1] * 30)
    assert s.ci_low <= s.mean <= s.ci_high
