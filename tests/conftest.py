from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from experiment_ledger import schemas
from experiment_ledger.config import Settings
from experiment_ledger.db import session_scope
from experiment_ledger.main import create_app
from experiment_ledger.models import Role
from experiment_ledger.services.users import UserService


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Throwaway secret generated per test run; never a fixed value.
    return Settings(
        jwt_secret=secrets.token_hex(32),
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        cors_origins="http://localhost:5173",
        login_rate_limit="5/60",
        log_json=True,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings, create_tables=True)
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


@pytest.fixture
def make_user(client: TestClient) -> Callable[[Role], tuple[str, str]]:
    def _make(role: Role) -> tuple[str, str]:
        email = f"{role.value}-{secrets.token_hex(4)}@example.com"
        password = secrets.token_urlsafe(16)
        factory = client.app.state.session_factory  # type: ignore[attr-defined]
        for session in session_scope(factory):
            UserService(session).create(
                schemas.UserCreate(email=email, password=password, role=role)
            )
        return email, password

    return _make


@pytest.fixture
def auth_header(
    client: TestClient, make_user: Callable[[Role], tuple[str, str]]
) -> Callable[[Role], dict[str, str]]:
    def _header(role: Role) -> dict[str, str]:
        email, password = make_user(role)
        resp = client.post("/auth/token", data={"username": email, "password": password})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    return _header


EXPERIMENT = {
    "key": "checkout-cta",
    "name": "Checkout CTA wording",
    "hypothesis": "Clearer CTA copy increases checkout conversion.",
    "metric_name": "checkout_conversion",
    "baseline_rate": 0.10,
    "mde_relative": 0.10,
}
