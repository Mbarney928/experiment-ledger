"""Command-line tools: migrations, demo seed data, user creation, offline analysis.

Seeded demo users get a random password generated at seed time and printed only to this
terminal. Nothing generated here is ever written to a file in the repo.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import numpy as np
import typer
from alembic import command
from alembic.config import Config
from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import Session

from experiment_ledger import schemas
from experiment_ledger.config import get_settings
from experiment_ledger.db import make_engine, make_session_factory, session_scope
from experiment_ledger.models import Role
from experiment_ledger.repositories import UserRepository
from experiment_ledger.services.experiments import ExperimentService, LookInput, analyze_looks
from experiment_ledger.services.users import UserService

app = typer.Typer(help="experiment-ledger admin CLI", no_args_is_help=True)
console = Console()

_ROOT = Path(__file__).resolve().parents[2]

# Fictional demo scenarios: (key, name, metric, baseline, mde, true_lift, srm_skew)
DEMO_EXPERIMENTS = [
    (
        "checkout-cta-copy",
        "Checkout CTA: 'Pay now' vs 'Complete order'",
        "checkout_conversion",
        0.042,
        0.15,
        0.22,
        0.0,
    ),
    (
        "onboarding-checklist",
        "Onboarding checklist on first login",
        "activation_7d",
        0.31,
        0.06,
        0.0,
        0.0,
    ),
    (
        "pricing-annual-default",
        "Default pricing toggle to annual billing",
        "paid_conversion",
        0.018,
        0.20,
        0.08,
        0.0,
    ),
    ("search-autocomplete", "Search autocomplete v2", "search_to_cart", 0.12, 0.08, 0.05, 0.03),
]


@contextmanager
def _session() -> Iterator[Session]:
    engine = make_engine(get_settings().database_url)
    try:
        yield from session_scope(make_session_factory(engine))
    finally:
        engine.dispose()


def _alembic_config() -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    command.upgrade(_alembic_config(), "head")
    console.print("[green]Database is up to date.[/green]")


@app.command("create-user")
def create_user(
    email: str,
    role: Annotated[Role, typer.Option(case_sensitive=False)] = Role.viewer,
) -> None:
    """Create a user with a freshly generated random password (shown once, here)."""
    password = secrets.token_urlsafe(18)
    with _session() as session:
        UserService(session).create(schemas.UserCreate(email=email, password=password, role=role))
    console.print(f"Created [bold]{email}[/bold] ({role}).")
    console.print(f"Password (shown once): {password}", soft_wrap=True, highlight=False)


@app.command()
def seed(seed: Annotated[int, typer.Option(help="RNG seed for reproducible data")] = 7) -> None:
    """Load fictional demo users and experiments (example.com addresses only)."""
    rng = np.random.default_rng(seed)
    creds: list[tuple[str, str, Role]] = []
    with _session() as session:
        users = UserService(session)
        accounts = {}
        for email, role in [
            ("admin@example.com", Role.admin),
            ("analyst@example.com", Role.analyst),
            ("viewer@example.com", Role.viewer),
        ]:
            existing = UserRepository(session).by_email(email)
            if existing:
                accounts[role] = existing
                continue
            password = secrets.token_urlsafe(18)
            accounts[role] = users.create(
                schemas.UserCreate(email=email, password=password, role=role)
            )
            creds.append((email, password, role))

        svc = ExperimentService(session)
        for key, name, metric, base, mde, lift, skew in DEMO_EXPERIMENTS:
            if svc.repo.by_key(key):
                continue
            exp = svc.create(
                schemas.ExperimentCreate(
                    key=key,
                    name=name,
                    metric_name=metric,
                    baseline_rate=base,
                    mde_relative=mde,
                    hypothesis=f"Changing this will move {metric} by "
                    f"at least {mde:.0%} relative to the current {base:.1%}.",
                ),
                accounts[Role.analyst],
            )
            svc.start(key)
            planned = exp.planned_sample_per_arm
            c_n = t_n = c_x = t_x = 0
            for _ in range(int(rng.integers(3, 6))):
                daily = int(planned * rng.uniform(0.18, 0.3))
                add_c = int(daily * (1 - skew))
                add_t = int(daily * (1 + skew))
                c_n, t_n = c_n + add_c, t_n + add_t
                c_x += int(rng.binomial(add_c, base))
                t_x += int(rng.binomial(add_t, min(0.99, base * (1 + lift))))
                svc.record_snapshot(
                    key,
                    schemas.SnapshotIn(
                        control_exposures=c_n,
                        control_conversions=c_x,
                        treatment_exposures=t_n,
                        treatment_conversions=t_x,
                    ),
                    accounts[Role.analyst],
                )

    console.print(f"[green]Seeded {len(DEMO_EXPERIMENTS)} demo experiments.[/green]")
    if creds:
        table = Table(title="Demo logins (local only, shown once)")
        for col in ("email", "role", "password"):
            table.add_column(col)
        for email, password, role in creds:
            table.add_row(email, role, password)
        console.print(table)


@app.command()
def analyze(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    planned_per_arm: Annotated[int, typer.Option(min=1)],
    alpha: Annotated[float, typer.Option(min=0.0001, max=0.4999)] = 0.05,
) -> None:
    """Analyze a JSON file of cumulative looks without a database."""
    raw = json.loads(path.read_text())
    looks = [LookInput(**schemas.SnapshotIn(**row).model_dump()) for row in raw]
    result = analyze_looks(path.stem, looks, planned_per_arm, alpha)
    table = Table(title=f"Looks for {path.name}")
    for col in ("look", "info", "z", "boundary", "crossed"):
        table.add_column(col, justify="right")
    for look in result.looks:
        table.add_row(
            str(look.look),
            f"{look.information_fraction:.0%}",
            f"{look.z:+.2f}",
            f"±{look.boundary:.2f}",
            "yes" if look.crossed else "",
        )
    console.print(table)
    console.print(f"[bold]{result.decision}[/bold]: {result.summary}")


if __name__ == "__main__":
    app()
