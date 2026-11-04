"""Experiment lifecycle and analysis: the business rules sit here, between API and repositories."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from experiment_ledger import schemas
from experiment_ledger.models import Experiment, ExperimentStatus, Snapshot, User
from experiment_ledger.repositories import ExperimentRepository
from experiment_ledger.services import stats
from experiment_ledger.services.errors import Conflict, InvalidState, NotFound

# A sample-ratio mismatch this extreme almost always means broken assignment or logging,
# so the effect estimate can't be trusted regardless of significance.
SRM_THRESHOLD = 0.001


@dataclass(frozen=True, slots=True)
class LookInput:
    control_exposures: int
    control_conversions: int
    treatment_exposures: int
    treatment_conversions: int


class ExperimentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ExperimentRepository(session)

    # ---- queries -------------------------------------------------------------------------
    def get(self, key: str) -> Experiment:
        experiment = self.repo.by_key(key)
        if experiment is None:
            raise NotFound(f"experiment '{key}' not found")
        return experiment

    def list(
        self, status: ExperimentStatus | None, limit: int, offset: int
    ) -> Sequence[Experiment]:
        return self.repo.list(status=status, limit=limit, offset=offset)

    # ---- commands ------------------------------------------------------------------------
    def create(self, data: schemas.ExperimentCreate, owner: User) -> Experiment:
        if self.repo.by_key(data.key) is not None:
            raise Conflict(f"experiment key '{data.key}' already exists")
        planned = stats.sample_size_per_arm(
            data.baseline_rate, data.mde_relative, data.alpha, data.power
        )
        experiment = Experiment(
            key=data.key,
            name=data.name,
            hypothesis=data.hypothesis,
            metric_name=data.metric_name,
            baseline_rate=data.baseline_rate,
            mde_relative=data.mde_relative,
            alpha=data.alpha,
            power=data.power,
            planned_sample_per_arm=planned,
            owner_id=owner.id,
            status=ExperimentStatus.draft,
        )
        try:
            return self.repo.add(experiment)
        except IntegrityError as exc:  # concurrent create with the same key
            self.session.rollback()
            raise Conflict(f"experiment key '{data.key}' already exists") from exc

    def start(self, key: str) -> Experiment:
        experiment = self.get(key)
        if experiment.status is not ExperimentStatus.draft:
            raise InvalidState(f"only draft experiments can be started (is {experiment.status})")
        experiment.status = ExperimentStatus.running
        experiment.started_at = datetime.now(UTC)
        return experiment

    def stop(self, key: str) -> Experiment:
        experiment = self.get(key)
        if experiment.status is not ExperimentStatus.running:
            raise InvalidState(f"only running experiments can be stopped (is {experiment.status})")
        experiment.status = ExperimentStatus.stopped
        experiment.stopped_at = datetime.now(UTC)
        return experiment

    def record_snapshot(self, key: str, data: schemas.SnapshotIn, user: User) -> Snapshot:
        experiment = self.get(key)
        if experiment.status is not ExperimentStatus.running:
            raise InvalidState("results can only be recorded while the experiment is running")
        if data.control_exposures == 0 or data.treatment_exposures == 0:
            raise InvalidState("both arms need at least one exposure")
        if experiment.snapshots:
            prev = experiment.snapshots[-1]
            fields = (
                "control_exposures",
                "control_conversions",
                "treatment_exposures",
                "treatment_conversions",
            )
            for name in fields:
                if getattr(data, name) < getattr(prev, name):
                    raise InvalidState(
                        f"{name} decreased; snapshots are cumulative and must not go backwards"
                    )
            prev_total = prev.control_exposures + prev.treatment_exposures
            if data.control_exposures + data.treatment_exposures <= prev_total:
                raise InvalidState("a new look needs more exposures than the previous one")
        snapshot = Snapshot(
            experiment_id=experiment.id, recorded_by_id=user.id, **data.model_dump()
        )
        self.repo.add_snapshot(snapshot)
        experiment.snapshots.append(snapshot)
        return snapshot

    # ---- analysis ------------------------------------------------------------------------
    def analyze(self, key: str) -> schemas.AnalysisOut:
        experiment = self.get(key)
        looks = [
            LookInput(
                s.control_exposures,
                s.control_conversions,
                s.treatment_exposures,
                s.treatment_conversions,
            )
            for s in experiment.snapshots
        ]
        return analyze_looks(
            experiment.key, looks, experiment.planned_sample_per_arm, experiment.alpha
        )


def analyze_looks(
    key: str, looks: Sequence[LookInput], planned_per_arm: int, alpha: float
) -> schemas.AnalysisOut:
    """Turn a sequence of cumulative looks into a decision. Pure function, easy to test."""
    if not looks:
        return schemas.AnalysisOut(
            experiment_key=key, decision="no_data", summary="No results recorded yet."
        )

    planned_total = 2 * planned_per_arm
    fractions: list[float] = []
    kept: list[LookInput] = []
    for look in looks:
        t = min(1.0, (look.control_exposures + look.treatment_exposures) / planned_total)
        if fractions and t <= fractions[-1]:
            continue  # data beyond the planned horizon is evaluated at the final boundary
        fractions.append(t)
        kept.append(look)

    boundaries = stats.sequential_boundaries(fractions, alpha)
    look_rows: list[schemas.LookOut] = []
    first_cross: schemas.LookOut | None = None
    for i, (look, t, b) in enumerate(zip(kept, fractions, boundaries, strict=True), start=1):
        res = stats.two_proportion_ztest(
            look.control_conversions,
            look.control_exposures,
            look.treatment_conversions,
            look.treatment_exposures,
            alpha,
        )
        row = schemas.LookOut(
            look=i, information_fraction=t, z=res.z, boundary=b, crossed=abs(res.z) >= b
        )
        look_rows.append(row)
        if row.crossed and first_cross is None:
            first_cross = row

    latest = looks[-1]
    res = stats.two_proportion_ztest(
        latest.control_conversions,
        latest.control_exposures,
        latest.treatment_conversions,
        latest.treatment_exposures,
        alpha,
    )
    srm = stats.srm_p_value(latest.control_exposures, latest.treatment_exposures)
    t_latest = fractions[-1]

    decision: schemas.Decision
    if srm < SRM_THRESHOLD:
        decision = "investigate_srm"
        summary = (
            f"Sample ratio mismatch (p={srm:.2g}): traffic split is not 50/50. "
            "Fix assignment or logging before trusting any result."
        )
    elif first_cross is not None:
        winner = "treatment" if first_cross.z > 0 else "control"
        decision = "stop_treatment_wins" if winner == "treatment" else "stop_control_wins"
        summary = (
            f"Stopped early at look {first_cross.look}: |z|={abs(first_cross.z):.2f} crossed the "
            f"sequential boundary {first_cross.boundary:.2f}. {winner.capitalize()} wins."
        )
    elif t_latest >= 1.0:
        decision = "stop_no_effect"
        summary = (
            "Planned sample reached without crossing the boundary: no detectable effect at the "
            "chosen minimum effect size."
        )
    else:
        decision = "continue"
        summary = (
            f"{t_latest:.0%} of planned sample collected; no boundary crossed. Keep running — "
            "the naive p-value is not valid yet."
        )

    def arm(conv: int, n: int) -> schemas.ArmOut:
        ci = stats.wilson_interval(conv, n, alpha)
        return schemas.ArmOut(
            exposures=n, conversions=conv, rate=conv / n, rate_ci_low=ci.low, rate_ci_high=ci.high
        )

    return schemas.AnalysisOut(
        experiment_key=key,
        decision=decision,
        summary=summary,
        control=arm(latest.control_conversions, latest.control_exposures),
        treatment=arm(latest.treatment_conversions, latest.treatment_exposures),
        absolute_diff=res.absolute_diff,
        relative_lift=res.relative_lift,
        diff_ci_low=res.diff_ci.low,
        diff_ci_high=res.diff_ci.high,
        fixed_horizon_p_value=res.p_value,
        srm_p_value=srm,
        information_fraction=t_latest,
        looks=look_rows,
    )
