"""HTTP routes. Thin: validate input, call a service, shape output."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import text
from sqlalchemy.orm import Session

from experiment_ledger import schemas
from experiment_ledger.api.deps import get_current_user, get_session, get_settings_dep, require_role
from experiment_ledger.config import Settings
from experiment_ledger.models import ExperimentStatus, Role, User
from experiment_ledger.security import create_access_token
from experiment_ledger.services import stats
from experiment_ledger.services.experiments import ExperimentService
from experiment_ledger.services.users import UserService

SessionDep = Annotated[Session, Depends(get_session)]
ERR: dict[int | str, dict[str, Any]] = {
    401: {"model": schemas.ErrorOut},
    403: {"model": schemas.ErrorOut},
    404: {"model": schemas.ErrorOut},
}

health = APIRouter(tags=["health"])
auth = APIRouter(prefix="/auth", tags=["auth"])
users = APIRouter(prefix="/users", tags=["users"])
planning = APIRouter(prefix="/planning", tags=["planning"])
experiments = APIRouter(prefix="/experiments", tags=["experiments"], responses=ERR)


@health.get("/health")
def healthcheck(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@auth.post(
    "/token", response_model=schemas.TokenOut, responses={**ERR, 429: {"model": schemas.ErrorOut}}
)
def login(
    request: Request,
    session: SessionDep,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> schemas.TokenOut:
    limiter = request.app.state.login_limiter
    client_key = request.client.host if request.client else "unknown"
    if not limiter.allow(client_key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts; try again later",
            headers={"Retry-After": str(limiter.retry_after(client_key))},
        )
    user = UserService(session).authenticate(form.username, form.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="incorrect email or password")
    token = create_access_token(
        user_id=user.id,
        role=user.role,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        minutes=settings.access_token_minutes,
    )
    return schemas.TokenOut(access_token=token, expires_in=settings.access_token_minutes * 60)


@auth.get("/me", response_model=schemas.UserOut)
def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user


@users.post("", response_model=schemas.UserOut, status_code=201, responses=ERR)
def create_user(
    data: schemas.UserCreate,
    session: SessionDep,
    _admin: Annotated[User, Depends(require_role(Role.admin))],
) -> User:
    user = UserService(session).create(data)
    session.commit()
    return user


@planning.post("/sample-size", response_model=schemas.SampleSizeOut, responses=ERR)
def sample_size(
    data: schemas.SampleSizeIn, _user: Annotated[User, Depends(get_current_user)]
) -> schemas.SampleSizeOut:
    n = stats.sample_size_per_arm(data.baseline_rate, data.mde_relative, data.alpha, data.power)
    return schemas.SampleSizeOut(sample_per_arm=n, total_sample=2 * n)


@experiments.get("", response_model=list[schemas.ExperimentOut])
def list_experiments(
    session: SessionDep,
    _user: Annotated[User, Depends(get_current_user)],
    status_filter: Annotated[ExperimentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[schemas.ExperimentOut]:
    rows = ExperimentService(session).list(status_filter, limit, offset)
    return [schemas.ExperimentOut.model_validate(r) for r in rows]


@experiments.post("", response_model=schemas.ExperimentOut, status_code=201)
def create_experiment(
    data: schemas.ExperimentCreate,
    session: SessionDep,
    user: Annotated[User, Depends(require_role(Role.analyst))],
) -> schemas.ExperimentOut:
    experiment = ExperimentService(session).create(data, user)
    session.commit()
    return schemas.ExperimentOut.model_validate(experiment)


@experiments.get("/{key}", response_model=schemas.ExperimentDetail)
def get_experiment(
    key: str, session: SessionDep, _user: Annotated[User, Depends(get_current_user)]
) -> schemas.ExperimentDetail:
    return schemas.ExperimentDetail.model_validate(ExperimentService(session).get(key))


@experiments.post("/{key}/start", response_model=schemas.ExperimentOut)
def start_experiment(
    key: str, session: SessionDep, _user: Annotated[User, Depends(require_role(Role.analyst))]
) -> schemas.ExperimentOut:
    experiment = ExperimentService(session).start(key)
    session.commit()
    return schemas.ExperimentOut.model_validate(experiment)


@experiments.post("/{key}/stop", response_model=schemas.ExperimentOut)
def stop_experiment(
    key: str, session: SessionDep, _user: Annotated[User, Depends(require_role(Role.analyst))]
) -> schemas.ExperimentOut:
    experiment = ExperimentService(session).stop(key)
    session.commit()
    return schemas.ExperimentOut.model_validate(experiment)


@experiments.post("/{key}/snapshots", response_model=schemas.SnapshotOut, status_code=201)
def record_snapshot(
    key: str,
    data: schemas.SnapshotIn,
    session: SessionDep,
    user: Annotated[User, Depends(require_role(Role.analyst))],
) -> schemas.SnapshotOut:
    snap = ExperimentService(session).record_snapshot(key, data, user)
    session.commit()
    return schemas.SnapshotOut.model_validate(snap)


@experiments.get("/{key}/analysis", response_model=schemas.AnalysisOut)
def analyze_experiment(
    key: str, session: SessionDep, _user: Annotated[User, Depends(get_current_user)]
) -> schemas.AnalysisOut:
    return ExperimentService(session).analyze(key)
