"""Startup configuration and security primitives."""

from __future__ import annotations

import secrets
import time

import jwt
import pytest
from pydantic import ValidationError

from experiment_ledger.config import Settings
from experiment_ledger.models import Role
from experiment_ledger.security import (
    InvalidToken,
    SlidingWindowRateLimiter,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_missing_secret_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError, match="jwt_secret"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "value", ["short", "change-me-generate-with-openssl-rand-hex-32", "CHANGEME" * 5]
)
def test_weak_or_placeholder_secret_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(jwt_secret=value, _env_file=None)  # type: ignore[call-arg]


def test_secret_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    value = secrets.token_hex(32)
    monkeypatch.setenv("JWT_SECRET", value)
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example.com, https://b.example.com")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.jwt_secret == value
    assert settings.cors_origin_list == ["https://a.example.com", "https://b.example.com"]


def test_rate_limit_format_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(jwt_secret=secrets.token_hex(32), login_rate_limit="lots", _env_file=None)  # type: ignore[call-arg]


def test_password_hashing() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed.startswith("$2b$")
    assert verify_password("correct horse battery", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("anything", None)
    with pytest.raises(ValueError, match="too long"):
        hash_password("x" * 73)


def test_jwt_roundtrip_and_rejections() -> None:
    key = secrets.token_hex(32)
    token = create_access_token(
        user_id=7, role=Role.analyst, secret=key, algorithm="HS256", minutes=5
    )
    assert decode_access_token(token, secret=key, algorithm="HS256") == (7, Role.analyst)
    with pytest.raises(InvalidToken):
        decode_access_token(token, secret=secrets.token_hex(32), algorithm="HS256")
    expired = jwt.encode({"sub": "7", "role": "admin", "exp": int(time.time()) - 10}, key, "HS256")
    with pytest.raises(InvalidToken):
        decode_access_token(expired, secret=key, algorithm="HS256")
    no_exp = jwt.encode({"sub": "7", "role": "admin"}, key, "HS256")
    with pytest.raises(InvalidToken):
        decode_access_token(no_exp, secret=key, algorithm="HS256")
    bad_role = jwt.encode({"sub": "7", "role": "root", "exp": int(time.time()) + 60}, key, "HS256")
    with pytest.raises(InvalidToken):
        decode_access_token(bad_role, secret=key, algorithm="HS256")


def test_alg_none_token_rejected() -> None:
    key = secrets.token_hex(32)
    unsigned = jwt.encode({"sub": "1", "role": "admin", "exp": int(time.time()) + 60}, None, "none")
    with pytest.raises(InvalidToken):
        decode_access_token(unsigned, secret=key, algorithm="HS256")


def test_sliding_window_limiter() -> None:
    limiter = SlidingWindowRateLimiter(2, 10)
    assert limiter.retry_after("ip") == 0
    assert limiter.allow("ip", now=0)
    assert limiter.allow("ip", now=1)
    assert not limiter.allow("ip", now=2)
    assert limiter.retry_after("ip", now=2) >= 8
    assert limiter.allow("other", now=2)
    assert limiter.allow("ip", now=10.5)  # first event expired
