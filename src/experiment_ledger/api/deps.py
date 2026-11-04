"""FastAPI dependencies: DB session, current user, role checks."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from experiment_ledger.config import Settings
from experiment_ledger.db import session_scope
from experiment_ledger.models import Role, User
from experiment_ledger.repositories import UserRepository
from experiment_ledger.security import InvalidToken, decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

_ROLE_RANK = {Role.viewer: 0, Role.analyst: 1, Role.admin: 2}


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_session(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        user_id, _role = decode_access_token(
            token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
    except InvalidToken:
        raise unauthorized from None
    user = UserRepository(session).get(user_id)
    if user is None:
        raise unauthorized
    return user


def require_role(minimum: Role) -> Callable[[User], User]:
    """Role from the database (not the token) is authoritative, so demotions apply instantly."""

    def checker(user: User = Depends(get_current_user)) -> User:
        if _ROLE_RANK[user.role] < _ROLE_RANK[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"requires {minimum} role")
        return user

    return checker
