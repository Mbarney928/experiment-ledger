"""Data-access layer. Repositories only talk to the database; no business rules live here."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from experiment_ledger.models import Experiment, ExperimentStatus, Snapshot, User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)

    def by_email(self, email: str) -> User | None:
        return self.session.scalar(select(User).where(func.lower(User.email) == email.lower()))

    def add(self, user: User) -> User:
        self.session.add(user)
        self.session.flush()
        return user


class ExperimentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def by_key(self, key: str) -> Experiment | None:
        stmt = (
            select(Experiment)
            .where(Experiment.key == key)
            .options(selectinload(Experiment.snapshots))
        )
        return self.session.scalar(stmt)

    def list(
        self, status: ExperimentStatus | None = None, limit: int = 50, offset: int = 0
    ) -> Sequence[Experiment]:
        stmt = select(Experiment).order_by(Experiment.created_at.desc(), Experiment.id.desc())
        if status is not None:
            stmt = stmt.where(Experiment.status == status)
        return self.session.scalars(stmt.limit(limit).offset(offset)).all()

    def add(self, experiment: Experiment) -> Experiment:
        self.session.add(experiment)
        self.session.flush()
        return experiment

    def add_snapshot(self, snapshot: Snapshot) -> Snapshot:
        self.session.add(snapshot)
        self.session.flush()
        return snapshot
