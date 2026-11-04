"""CLI smoke tests: migrations, seeding and offline analysis run end to end."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

import pytest
from typer.testing import CliRunner

from experiment_ledger.cli import app
from experiment_ledger.config import get_settings

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "cli.db"
    monkeypatch.setenv("JWT_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.chdir(tmp_path)  # ensure no stray .env is read
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def test_migrate_seed_and_create_user(cli_env: Path) -> None:
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    seeded = runner.invoke(app, ["seed"])
    assert seeded.exit_code == 0, seeded.output
    assert "Seeded 4 demo experiments" in seeded.output
    assert "admin@example.com" in seeded.output
    # Second run is idempotent and prints no new credentials.
    again = runner.invoke(app, ["seed"])
    assert again.exit_code == 0
    assert "Demo logins" not in again.output

    created = runner.invoke(app, ["create-user", "ops@example.com", "--role", "analyst"])
    assert created.exit_code == 0, created.output
    assert re.search(r"Password \(shown once\): \S{20,}", created.output)


def test_offline_analyze(cli_env: Path, tmp_path: Path) -> None:
    looks = [
        {
            "control_exposures": 3000,
            "control_conversions": 300,
            "treatment_exposures": 3000,
            "treatment_conversions": 350,
        },
        {
            "control_exposures": 9000,
            "control_conversions": 900,
            "treatment_exposures": 9000,
            "treatment_conversions": 1080,
        },
    ]
    path = tmp_path / "looks.json"
    path.write_text(json.dumps(looks))
    result = runner.invoke(app, ["analyze", str(path), "--planned-per-arm", "14751"])
    assert result.exit_code == 0, result.output
    assert "stop_treatment_wins" in result.output
