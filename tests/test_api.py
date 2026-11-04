"""Integration tests through the HTTP layer with a real (temporary) SQLite database."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from experiment_ledger.models import Role
from tests.conftest import EXPERIMENT

Header = Callable[[Role], dict[str, str]]


def _running(client: TestClient, h: dict[str, str]) -> None:
    assert client.post("/experiments", json=EXPERIMENT, headers=h).status_code == 201
    assert client.post(f"/experiments/{EXPERIMENT['key']}/start", headers=h).status_code == 200


def _snap(cn: int, cx: int, tn: int, tx: int) -> dict[str, int]:
    return {
        "control_exposures": cn,
        "control_conversions": cx,
        "treatment_exposures": tn,
        "treatment_conversions": tx,
    }


def test_health_and_security_headers(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.json() == {"status": "ok"}
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
    assert len(resp.headers["X-Request-ID"]) == 32


def test_docs_are_served_with_relaxed_csp(client: TestClient) -> None:
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "Content-Security-Policy" not in resp.headers
    assert "/experiments/{key}/analysis" in client.get("/openapi.json").json()["paths"]


def test_cors_only_allows_configured_origin(client: TestClient) -> None:
    pre = {"Access-Control-Request-Method": "GET"}
    ok = client.options("/health", headers={"Origin": "http://localhost:5173", **pre})
    bad = client.options("/health", headers={"Origin": "https://evil.example.org", **pre})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers


def test_protected_routes_require_token(client: TestClient) -> None:
    for method, path in [
        ("get", "/experiments"),
        ("post", "/experiments"),
        ("get", "/experiments/x/analysis"),
        ("post", "/planning/sample-size"),
        ("get", "/auth/me"),
        ("post", "/users"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path


def test_garbage_and_tampered_tokens_rejected(client: TestClient, auth_header: Header) -> None:
    assert client.get("/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    token = auth_header(Role.viewer)["Authorization"]
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert client.get("/auth/me", headers={"Authorization": tampered}).status_code == 401


def test_login_wrong_password_is_generic(client: TestClient, make_user: Callable) -> None:  # type: ignore[type-arg]
    email, _ = make_user(Role.viewer)
    wrong = client.post("/auth/token", data={"username": email, "password": "x" * 16})
    unknown = client.post(
        "/auth/token", data={"username": "nobody@example.com", "password": "x" * 16}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_login_is_rate_limited(client: TestClient) -> None:
    codes = [
        client.post(
            "/auth/token", data={"username": "a@example.com", "password": "x" * 16}
        ).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_validation_errors_do_not_echo_input(client: TestClient, auth_header: Header) -> None:
    h = auth_header(Role.admin)
    secret_like = "Sup3r-s3cret-password-value-that-is-way-too-long-" * 3
    resp = client.post(
        "/users", json={"email": "new@example.com", "password": secret_like}, headers=h
    )
    assert resp.status_code == 422
    assert secret_like not in resp.text


def test_role_based_access(client: TestClient, auth_header: Header) -> None:
    viewer = auth_header(Role.viewer)
    analyst = auth_header(Role.analyst)
    assert client.post("/experiments", json=EXPERIMENT, headers=viewer).status_code == 403
    assert client.post("/experiments", json=EXPERIMENT, headers=analyst).status_code == 201
    assert client.get("/experiments", headers=viewer).status_code == 200
    user = {"email": "new@example.com", "password": "a-long-enough-pass"}
    assert client.post("/users", json=user, headers=analyst).status_code == 403


def test_admin_creates_users_and_duplicates_conflict(
    client: TestClient, auth_header: Header
) -> None:
    h = auth_header(Role.admin)
    body = {"email": "New.Person@Example.com", "password": "a-long-enough-pass", "role": "analyst"}
    created = client.post("/users", json=body, headers=h)
    assert created.status_code == 201
    assert created.json()["email"] == "new.person@example.com"
    assert "password" not in created.text
    assert "hash" not in created.text
    assert client.post("/users", json=body, headers=h).status_code == 409
    login = client.post(
        "/auth/token", data={"username": "new.person@example.com", "password": body["password"]}
    )
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert me.json()["role"] == "analyst"


def test_sample_size_endpoint(client: TestClient, auth_header: Header) -> None:
    h = auth_header(Role.viewer)
    resp = client.post(
        "/planning/sample-size", json={"baseline_rate": 0.1, "mde_relative": 0.1}, headers=h
    )
    assert resp.json() == {"sample_per_arm": 14751, "total_sample": 29502}
    bad = client.post(
        "/planning/sample-size", json={"baseline_rate": 0.8, "mde_relative": 0.5}, headers=h
    )
    assert bad.status_code == 422


def test_experiment_input_validation(client: TestClient, auth_header: Header) -> None:
    h = auth_header(Role.analyst)
    for override in [
        {"key": "Not A Slug"},
        {"baseline_rate": 1.5},
        {"mde_relative": 0},
        {"alpha": 0.9},
        {"hypothesis": "short"},
    ]:
        resp = client.post("/experiments", json={**EXPERIMENT, **override}, headers=h)
        assert resp.status_code == 422, override


def test_full_lifecycle_and_analysis(client: TestClient, auth_header: Header) -> None:
    h = auth_header(Role.analyst)
    created = client.post("/experiments", json=EXPERIMENT, headers=h).json()
    assert created["planned_sample_per_arm"] == 14751
    assert created["status"] == "draft"
    key = EXPERIMENT["key"]
    assert client.post("/experiments", json=EXPERIMENT, headers=h).status_code == 409

    # Can't record results before starting.
    assert (
        client.post(
            f"/experiments/{key}/snapshots", json=_snap(10, 1, 10, 1), headers=h
        ).status_code
        == 409
    )
    assert client.get(f"/experiments/{key}/analysis", headers=h).json()["decision"] == "no_data"

    client.post(f"/experiments/{key}/start", headers=h)
    assert client.post(f"/experiments/{key}/start", headers=h).status_code == 409

    # Look 1 (~20%): a big but early difference. Naive p < .05, but sequential says continue.
    s1 = _snap(3000, 300, 3000, 350)
    assert client.post(f"/experiments/{key}/snapshots", json=s1, headers=h).status_code == 201
    a1 = client.get(f"/experiments/{key}/analysis", headers=h).json()
    assert a1["fixed_horizon_p_value"] < 0.05
    assert a1["decision"] == "continue"
    assert a1["looks"][0]["crossed"] is False

    # Look 2 (~60%): strong effect crosses the stricter boundary.
    s2 = _snap(9000, 900, 9000, 1080)
    client.post(f"/experiments/{key}/snapshots", json=s2, headers=h)
    a2 = client.get(f"/experiments/{key}/analysis", headers=h).json()
    assert a2["decision"] == "stop_treatment_wins"
    assert a2["relative_lift"] > 0.15
    assert a2["diff_ci_low"] > 0
    assert [lk["look"] for lk in a2["looks"]] == [1, 2]

    detail = client.get(f"/experiments/{key}", headers=h).json()
    assert len(detail["snapshots"]) == 2
    listed = client.get("/experiments?status=running", headers=h).json()
    assert [e["key"] for e in listed] == [key]

    stopped = client.post(f"/experiments/{key}/stop", headers=h).json()
    assert stopped["status"] == "stopped"
    assert stopped["stopped_at"] is not None
    assert client.post(f"/experiments/{key}/snapshots", json=s2, headers=h).status_code == 409


def test_snapshot_rules(client: TestClient, auth_header: Header) -> None:
    h = auth_header(Role.analyst)
    _running(client, h)
    url = f"/experiments/{EXPERIMENT['key']}/snapshots"
    assert client.post(url, json=_snap(10, 11, 10, 1), headers=h).status_code == 422
    assert client.post(url, json=_snap(0, 0, 10, 1), headers=h).status_code == 409
    assert client.post(url, json=_snap(100, 10, 100, 10), headers=h).status_code == 201
    # Going backwards or not adding exposures is rejected.
    assert client.post(url, json=_snap(90, 10, 120, 12), headers=h).status_code == 409
    assert client.post(url, json=_snap(100, 11, 100, 11), headers=h).status_code == 409


def test_unknown_experiment_404(client: TestClient, auth_header: Header) -> None:
    h = auth_header(Role.analyst)
    for method, path in [
        ("get", "/experiments/missing"),
        ("get", "/experiments/missing/analysis"),
        ("post", "/experiments/missing/start"),
        ("post", "/experiments/missing/stop"),
    ]:
        resp = getattr(client, method)(path, headers=h)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "experiment 'missing' not found"}


def test_stop_requires_running(client: TestClient, auth_header: Header) -> None:
    h = auth_header(Role.analyst)
    client.post("/experiments", json=EXPERIMENT, headers=h)
    assert client.post(f"/experiments/{EXPERIMENT['key']}/stop", headers=h).status_code == 409
