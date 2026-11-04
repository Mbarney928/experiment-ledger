"""Decision logic of the analysis service, tested as a pure function."""

from __future__ import annotations

from experiment_ledger.services.experiments import LookInput, analyze_looks

PLANNED = 10_000


def test_no_looks() -> None:
    assert analyze_looks("k", [], PLANNED, 0.05).decision == "no_data"


def test_srm_takes_priority_over_significance() -> None:
    look = LookInput(9000, 900, 10_000, 1300)
    result = analyze_looks("k", [look], PLANNED, 0.05)
    assert result.decision == "investigate_srm"
    assert result.srm_p_value is not None
    assert result.srm_p_value < 0.001


def test_control_can_win() -> None:
    result = analyze_looks("k", [LookInput(6000, 720, 6000, 540)], PLANNED, 0.05)
    assert result.decision == "stop_control_wins"
    assert result.relative_lift is not None
    assert result.relative_lift < 0


def test_no_effect_at_full_sample() -> None:
    looks = [LookInput(5000, 500, 5000, 505), LookInput(10_000, 1000, 10_000, 1010)]
    result = analyze_looks("k", looks, PLANNED, 0.05)
    assert result.decision == "stop_no_effect"
    assert result.information_fraction == 1.0


def test_looks_past_full_sample_are_collapsed() -> None:
    looks = [
        LookInput(5000, 500, 5000, 505),
        LookInput(10_000, 1000, 10_000, 1010),
        LookInput(12_000, 1200, 12_000, 1215),
    ]
    result = analyze_looks("k", looks, PLANNED, 0.05)
    assert len(result.looks) == 2  # the third look reuses the final boundary
    assert result.control is not None
    assert result.control.exposures == 12_000


def test_wilson_intervals_reported_per_arm() -> None:
    result = analyze_looks("k", [LookInput(1000, 100, 1000, 110)], PLANNED, 0.05)
    assert result.control is not None
    assert result.control.rate_ci_low < 0.1 < result.control.rate_ci_high
    assert result.decision == "continue"
