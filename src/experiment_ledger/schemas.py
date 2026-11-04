"""Request/response models. All API input is validated here before reaching services."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from experiment_ledger.models import ExperimentStatus, Role

KEY_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=72)
    role: Role = Role.viewer


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    role: Role


class SampleSizeIn(BaseModel):
    baseline_rate: float = Field(gt=0, lt=1, examples=[0.042])
    mde_relative: float = Field(
        gt=-1, le=5, examples=[0.10], description="Relative lift to detect, e.g. 0.10 = +10%"
    )
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    power: float = Field(default=0.8, gt=0.5, lt=1)

    @model_validator(mode="after")
    def _check(self) -> SampleSizeIn:
        if self.mde_relative == 0:
            raise ValueError("mde_relative must be non-zero")
        if not 0 < self.baseline_rate * (1 + self.mde_relative) < 1:
            raise ValueError("baseline_rate * (1 + mde_relative) must stay between 0 and 1")
        return self


class SampleSizeOut(BaseModel):
    sample_per_arm: int
    total_sample: int


class ExperimentCreate(SampleSizeIn):
    key: str = Field(min_length=3, max_length=64, pattern=KEY_PATTERN, examples=["checkout-cta"])
    name: str = Field(min_length=3, max_length=200)
    hypothesis: str = Field(min_length=10, max_length=4000)
    metric_name: str = Field(min_length=2, max_length=100, examples=["checkout_conversion"])


class SnapshotIn(BaseModel):
    control_exposures: int = Field(ge=0, le=10**10)
    control_conversions: int = Field(ge=0, le=10**10)
    treatment_exposures: int = Field(ge=0, le=10**10)
    treatment_conversions: int = Field(ge=0, le=10**10)

    @model_validator(mode="after")
    def _check(self) -> SnapshotIn:
        if self.control_conversions > self.control_exposures:
            raise ValueError("control_conversions cannot exceed control_exposures")
        if self.treatment_conversions > self.treatment_exposures:
            raise ValueError("treatment_conversions cannot exceed treatment_exposures")
        return self


class SnapshotOut(SnapshotIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    recorded_at: datetime


class ExperimentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    name: str
    hypothesis: str
    metric_name: str
    baseline_rate: float
    mde_relative: float
    alpha: float
    power: float
    planned_sample_per_arm: int
    status: ExperimentStatus
    created_at: datetime
    started_at: datetime | None
    stopped_at: datetime | None


class ExperimentDetail(ExperimentOut):
    snapshots: list[SnapshotOut]


Decision = Literal[
    "no_data",
    "continue",
    "stop_treatment_wins",
    "stop_control_wins",
    "stop_no_effect",
    "investigate_srm",
]


class LookOut(BaseModel):
    look: int
    information_fraction: float
    z: float
    boundary: float
    crossed: bool


class ArmOut(BaseModel):
    exposures: int
    conversions: int
    rate: float
    rate_ci_low: float
    rate_ci_high: float


class AnalysisOut(BaseModel):
    experiment_key: str
    decision: Decision
    summary: str
    control: ArmOut | None = None
    treatment: ArmOut | None = None
    absolute_diff: float | None = None
    relative_lift: float | None = None
    diff_ci_low: float | None = None
    diff_ci_high: float | None = None
    fixed_horizon_p_value: float | None = Field(
        default=None,
        description="Naive p-value. Only valid at the planned sample size; shown for context.",
    )
    srm_p_value: float | None = None
    information_fraction: float | None = None
    looks: list[LookOut] = []


class ErrorOut(BaseModel):
    detail: str
