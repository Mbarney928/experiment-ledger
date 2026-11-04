"""Application settings, loaded from environment variables and validated at startup.

There are deliberately no fallback secrets: if JWT_SECRET is missing, too short, or still the
placeholder from .env.example, the app refuses to start.
"""

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER = re.compile(r"change-?me", re.IGNORECASE)
_RATE = re.compile(r"^\d+/\d+$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = Field(default=60, ge=1, le=24 * 60)
    database_url: str = "sqlite:///./experiment_ledger.db"
    cors_origins: str = ""
    login_rate_limit: str = "5/60"
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder(cls, value: str) -> str:
        if _PLACEHOLDER.search(value):
            raise ValueError(
                "JWT_SECRET is still the .env.example placeholder; "
                "generate one with `openssl rand -hex 32`"
            )
        return value

    @field_validator("login_rate_limit")
    @classmethod
    def _valid_rate(cls, value: str) -> str:
        if not _RATE.match(value):
            raise ValueError("LOGIN_RATE_LIMIT must look like '<attempts>/<seconds>', e.g. 5/60")
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def login_rate(self) -> tuple[int, int]:
        attempts, seconds = self.login_rate_limit.split("/")
        return int(attempts), int(seconds)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
