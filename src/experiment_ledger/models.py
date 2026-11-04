"""ORM models.

Snapshots store *cumulative* counts per arm. Each snapshot is one "look" at the data, which is
what the sequential test needs to know how often the team has peeked.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from experiment_ledger.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Role(enum.StrEnum):
    viewer = "viewer"
    analyst = "analyst"
    admin = "admin"


class ExperimentStatus(enum.StrEnum):
    draft = "draft"
    running = "running"
    stopped = "stopped"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint("baseline_rate > 0 AND baseline_rate < 1", name="ck_baseline_rate"),
        CheckConstraint("alpha > 0 AND alpha < 0.5", name="ck_alpha"),
        CheckConstraint("power > 0.5 AND power < 1", name="ck_power"),
        CheckConstraint("planned_sample_per_arm > 0", name="ck_planned_sample"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    hypothesis: Mapped[str] = mapped_column(Text)
    metric_name: Mapped[str] = mapped_column(String(100))
    baseline_rate: Mapped[float] = mapped_column(Float)
    mde_relative: Mapped[float] = mapped_column(Float)
    alpha: Mapped[float] = mapped_column(Float)
    power: Mapped[float] = mapped_column(Float)
    planned_sample_per_arm: Mapped[int] = mapped_column(Integer)
    status: Mapped[ExperimentStatus] = mapped_column(
        Enum(ExperimentStatus, native_enum=False, length=16), default=ExperimentStatus.draft
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped[User] = relationship()
    snapshots: Mapped[list[Snapshot]] = relationship(
        back_populates="experiment",
        order_by="Snapshot.id",
        cascade="all, delete-orphan",
    )


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        CheckConstraint("control_conversions <= control_exposures", name="ck_control_counts"),
        CheckConstraint("treatment_conversions <= treatment_exposures", name="ck_treatment_counts"),
        CheckConstraint(
            "control_conversions >= 0 AND treatment_conversions >= 0", name="ck_nonnegative"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    control_exposures: Mapped[int] = mapped_column(Integer)
    control_conversions: Mapped[int] = mapped_column(Integer)
    treatment_exposures: Mapped[int] = mapped_column(Integer)
    treatment_conversions: Mapped[int] = mapped_column(Integer)

    experiment: Mapped[Experiment] = relationship(back_populates="snapshots")
