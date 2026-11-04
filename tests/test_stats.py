"""Stats engine tests against reference values and simulation."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats as sps

from experiment_ledger.services import stats


def test_ztest_matches_textbook_example() -> None:
    r = stats.two_proportion_ztest(100, 1000, 130, 1000)
    assert r.control_rate == pytest.approx(0.10)
    assert r.treatment_rate == pytest.approx(0.13)
    assert r.relative_lift == pytest.approx(0.30)
    assert r.z == pytest.approx(2.1027, abs=1e-4)
    assert r.p_value == pytest.approx(0.03549, abs=1e-5)
    assert r.diff_ci.low == pytest.approx(0.00207, abs=1e-5)
    assert r.diff_ci.high == pytest.approx(0.05793, abs=1e-5)


def test_ztest_degenerate_all_zero_is_not_significant() -> None:
    r = stats.two_proportion_ztest(0, 50, 0, 50)
    assert r.z == 0.0
    assert r.p_value == 1.0
    assert r.relative_lift is None


@pytest.mark.parametrize(
    ("args", "msg"),
    [((0, 0, 1, 10), "exposure"), ((11, 10, 1, 10), "between"), ((1, 10, -1, 10), "between")],
)
def test_ztest_rejects_invalid_counts(args: tuple[int, int, int, int], msg: str) -> None:
    with pytest.raises(stats.StatsError, match=msg):
        stats.two_proportion_ztest(*args)


def test_wilson_interval_reference_and_edges() -> None:
    ci = stats.wilson_interval(10, 100)
    assert ci.low == pytest.approx(0.05523, abs=1e-4)
    assert ci.high == pytest.approx(0.17437, abs=1e-4)
    zero = stats.wilson_interval(0, 20)
    assert zero.low == 0.0
    assert zero.high > 0
    with pytest.raises(stats.StatsError):
        stats.wilson_interval(1, 0)
    with pytest.raises(stats.StatsError):
        stats.wilson_interval(5, 4)


def test_sample_size_matches_standard_formula() -> None:
    # 10% baseline, +10% relative (10% -> 11%), alpha .05, power .8: textbook ~14,751 per arm.
    assert stats.sample_size_per_arm(0.10, 0.10) == 14751
    # Smaller effects need more traffic; higher power needs more traffic.
    assert stats.sample_size_per_arm(0.10, 0.05) > stats.sample_size_per_arm(0.10, 0.10)
    assert stats.sample_size_per_arm(0.10, 0.10, power=0.9) > 14751


@pytest.mark.parametrize(("base", "mde"), [(0.0, 0.1), (1.0, 0.1), (0.5, 0.0), (0.8, 0.5)])
def test_sample_size_rejects_invalid(base: float, mde: float) -> None:
    with pytest.raises(stats.StatsError):
        stats.sample_size_per_arm(base, mde)


def test_srm() -> None:
    assert stats.srm_p_value(5000, 5000) == pytest.approx(1.0)
    assert stats.srm_p_value(5000, 5400) < 0.001
    with pytest.raises(stats.StatsError):
        stats.srm_p_value(0, 0)


def test_spending_function_properties() -> None:
    assert stats.obf_spending(0, 0.05) == 0.0
    assert stats.obf_spending(1.0, 0.05) == pytest.approx(0.05)
    assert stats.obf_spending(2.0, 0.05) == pytest.approx(0.05)  # capped at t=1
    values = [stats.obf_spending(t, 0.05) for t in np.linspace(0.1, 1, 10)]
    assert values == sorted(values)


def test_single_look_boundary_is_fixed_horizon_critical_value() -> None:
    assert stats.sequential_boundaries([1.0]) == [pytest.approx(1.95996, abs=1e-4)]
    assert stats.sequential_boundaries([]) == []


def test_two_look_boundaries_spend_exactly_alpha() -> None:
    b1, b2 = stats.sequential_boundaries([0.5, 1.0])
    # First look: entire spend at t=0.5 is a normal quantile.
    assert 2 * sps.norm.sf(b1) == pytest.approx(stats.obf_spending(0.5, 0.05), rel=1e-6)
    # Overall: P(|Z1|<b1, |Z2|<b2) under H0 with corr sqrt(0.5) must equal 1 - alpha.
    r = math.sqrt(0.5)
    mvn = sps.multivariate_normal([0, 0], [[1, r], [r, 1]])
    inside = mvn.cdf([b1, b2]) - mvn.cdf([-b1, b2]) - mvn.cdf([b1, -b2]) + mvn.cdf([-b1, -b2])
    assert 1 - inside == pytest.approx(0.05, abs=2e-4)
    assert b1 > b2 > 1.96  # early looks are stricter; final look costs a little


def test_sequential_test_controls_type_one_error_under_peeking() -> None:
    rng = np.random.default_rng(42)
    ts = np.linspace(0.2, 1.0, 5)
    bounds = np.array(stats.sequential_boundaries(list(ts)))
    increments = rng.standard_normal((200_000, 5)) * np.sqrt(np.diff(np.r_[0, ts]))
    z = np.cumsum(increments, axis=1) / np.sqrt(ts)
    sequential_fpr = (np.abs(z) >= bounds).any(axis=1).mean()
    naive_fpr = (np.abs(z) >= 1.96).any(axis=1).mean()
    assert sequential_fpr == pytest.approx(0.05, abs=0.003)
    assert naive_fpr > 0.12  # what happens if you just check p < .05 every time


@pytest.mark.parametrize("fractions", [[0.5, 0.5], [0.6, 0.4], [0.0, 1.0], [0.5, 1.2]])
def test_sequential_rejects_bad_fractions(fractions: list[float]) -> None:
    with pytest.raises(stats.StatsError):
        stats.sequential_boundaries(fractions)


def test_tiny_first_look_gets_effectively_infinite_boundary() -> None:
    b = stats.sequential_boundaries([1e-4, 1.0])
    assert b[0] > 10
    assert b[1] == pytest.approx(1.96, abs=0.01)
