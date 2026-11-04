"""User management and authentication."""

from __future__ import annotations

from sqlalchemy.orm import Session

from experiment_ledger import schemas
from experiment_ledger.models import User
from experiment_ledger.repositories import UserRepository
from experiment_ledger.security import hash_password, verify_password
from experiment_ledger.services.errors import Conflict


class UserService:
    def __init__(self, session: Session) -> None:
        self.repo = UserRepository(session)

    def create(self, data: schemas.UserCreate) -> User:
        if self.repo.by_email(data.email) is not None:
            raise Conflict("a user with that email already exists")
        user = User(
            email=data.email.lower(), password_hash=hash_password(data.password), role=data.role
        )
        return self.repo.add(user)

    def authenticate(self, email: str, password: str) -> User | None:
        user = self.repo.by_email(email)
        ok = verify_password(password, user.password_hash if user else None)
        return user if ok else None
