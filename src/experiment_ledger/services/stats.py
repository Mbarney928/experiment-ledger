"""Pure statistics for two-arm conversion-rate experiments. No I/O, no ORM.

Everything here is deterministic and unit-tested against published reference values.

Methods
-------
* Two-proportion z-test (pooled variance) for the hypothesis test.
* Wald interval on the absolute difference (unpooled variance) for effect size.
* Wilson score interval for each arm's own rate (behaves well near 0 and 1).
* Sample-size planning for a two-sided test at a given alpha, power and relative MDE.
* Sample-ratio-mismatch (SRM) check: chi-square goodness-of-fit against a 50/50 split.
* Group-sequential boundaries using the Lan-DeMets O'Brien-Fleming-type alpha-spending function,
  computed by numerical integration (Armitage-McPherson-Rowe recursion). This is what lets a team
  look at results repeatedly without inflating the false-positive rate.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import optimize, stats

_GRID_POINTS = 4001


class StatsError(ValueError):
    """Raised for statistically meaningless inputs (e.g. zero exposures)."""


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class ZTestResult:
    control_rate: float
    treatment_rate: float
    absolute_diff: float
    relative_lift: float | None
    z: float
    p_value: float
    diff_ci: Interval


def wilson_interval(conversions: int, exposures: int, alpha: float = 0.05) -> Interval:
    if exposures <= 0:
        raise StatsError("exposures must be positive")
    if not 0 <= conversions <= exposures:
        raise StatsError("conversions must be between 0 and exposures")
    z = float(stats.norm.ppf(1 - alpha / 2))
    p = conversions / exposures
    denom = 1 + z**2 / exposures
    centre = (p + z**2 / (2 * exposures)) / denom
    half = z * math.sqrt(p * (1 - p) / exposures + z**2 / (4 * exposures**2)) / denom
    return Interval(max(0.0, centre - half), min(1.0, centre + half))


def two_proportion_ztest(
    control_conv: int,
    control_n: int,
    treatment_conv: int,
    treatment_n: int,
    alpha: float = 0.05,
) -> ZTestResult:
    if control_n <= 0 or treatment_n <= 0:
        raise StatsError("both arms need at least one exposure")
    if not (0 <= control_conv <= control_n and 0 <= treatment_conv <= treatment_n):
        raise StatsError("conversions must be between 0 and exposures")

    p_c = control_conv / control_n
    p_t = treatment_conv / treatment_n
    diff = p_t - p_c

    pooled = (control_conv + treatment_conv) / (control_n + treatment_n)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / control_n + 1 / treatment_n))
    if se_pooled == 0:
        z, p_value = 0.0, 1.0
    else:
        z = diff / se_pooled
        p_value = float(2 * stats.norm.sf(abs(z)))

    se_unpooled = math.sqrt(p_c * (1 - p_c) / control_n + p_t * (1 - p_t) / treatment_n)
    z_crit = float(stats.norm.ppf(1 - alpha / 2))
    ci = Interval(diff - z_crit * se_unpooled, diff + z_crit * se_unpooled)

    lift = diff / p_c if p_c > 0 else None
    return ZTestResult(p_c, p_t, diff, lift, z, p_value, ci)


def sample_size_per_arm(
    baseline_rate: float, mde_relative: float, alpha: float = 0.05, power: float = 0.8
) -> int:
    """Visitors needed per arm to detect a relative lift of ``mde_relative`` (two-sided)."""
    if not 0 < baseline_rate < 1:
        raise StatsError("baseline_rate must be between 0 and 1")
    if mde_relative == 0:
        raise StatsError("mde_relative must be non-zero")
    p1 = baseline_rate
    p2 = baseline_rate * (1 + mde_relative)
    if not 0 < p2 < 1:
        raise StatsError("baseline_rate * (1 + mde_relative) must stay between 0 and 1")
    z_a = float(stats.norm.ppf(1 - alpha / 2))
    z_b = float(stats.norm.ppf(power))
    p_bar = (p1 + p2) / 2
    numerator = (
        z_a * math.sqrt(2 * p_bar * (1 - p_bar)) + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
    ) ** 2
    return math.ceil(numerator / (p2 - p1) ** 2)


def srm_p_value(control_n: int, treatment_n: int) -> float:
    """Chi-square goodness-of-fit p-value against an intended 50/50 split."""
    total = control_n + treatment_n
    if total <= 0:
        raise StatsError("no exposures")
    expected = total / 2
    chi2 = ((control_n - expected) ** 2 + (treatment_n - expected) ** 2) / expected
    return float(stats.chi2.sf(chi2, df=1))


def obf_spending(t: float, alpha: float) -> float:
    """Lan-DeMets O'Brien-Fleming-type cumulative alpha spent at information fraction ``t``."""
    if t <= 0:
        return 0.0
    t = min(t, 1.0)
    z = float(stats.norm.ppf(1 - alpha / 2))
    return float(2 - 2 * stats.norm.cdf(z / math.sqrt(t)))


def sequential_boundaries(fractions: Sequence[float], alpha: float = 0.05) -> list[float]:
    """Two-sided z-boundaries for looks at the given cumulative information fractions.

    The test statistic Z_k = S_k / sqrt(t_k), where S is standard Brownian motion on [0, 1].
    For each look we find b_k so that the probability of first crossing |Z| >= b at look k,
    under H0, equals the alpha newly spent between t_{k-1} and t_k.
    """
    if not fractions:
        return []
    ts = [float(t) for t in fractions]
    if any(t <= 0 or t > 1 for t in ts) or any(b <= a for a, b in itertools.pairwise(ts)):
        raise StatsError("information fractions must be strictly increasing within (0, 1]")

    boundaries: list[float] = []
    spent_prev = 0.0
    grid: np.ndarray | None = None  # S values on the continuation region of the previous look
    density: np.ndarray | None = None  # sub-density of S on that grid (not yet stopped)
    t_prev = 0.0

    for t in ts:
        spent = obf_spending(t, alpha)
        increment = spent - spent_prev
        dt = t - t_prev

        if grid is None or density is None:
            # First look: S_1 ~ N(0, t) so the boundary is a plain normal quantile.
            b = float(stats.norm.isf(increment / 2)) if increment > 0 else math.inf
        else:
            sd = math.sqrt(dt)
            g, d = grid, density
            dx = g[1] - g[0]

            def cross_prob(
                b_z: float,
                *,
                sd: float = sd,
                g: np.ndarray = g,
                d: np.ndarray = d,
                dx: float = dx,
                t: float = t,
            ) -> float:
                c = b_z * math.sqrt(t)
                tail = stats.norm.sf((c - g) / sd) + stats.norm.cdf((-c - g) / sd)
                return float(np.sum(tail * d) * dx)

            if increment <= 1e-15:
                b = math.inf
            else:
                b = float(
                    optimize.brentq(
                        lambda x, inc=increment: cross_prob(x) - inc, 1e-6, 40.0, xtol=1e-10
                    )
                )
        boundaries.append(b)

        # Propagate the not-yet-stopped density to this look's continuation region.
        c = (b if math.isfinite(b) else 12.0) * math.sqrt(t)
        new_grid = np.linspace(-c, c, _GRID_POINTS)
        if grid is None or density is None:
            new_density = stats.norm.pdf(new_grid / math.sqrt(t)) / math.sqrt(t)
        else:
            sd = math.sqrt(dt)
            dx = grid[1] - grid[0]
            kernel = stats.norm.pdf((new_grid[:, None] - grid[None, :]) / sd) / sd
            new_density = kernel @ density * dx
        grid, density, t_prev, spent_prev = new_grid, new_density, t, spent

    return boundaries
