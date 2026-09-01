"""Canonical app runtime admission; all transports and PJUD effects synthetic."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.runtime_fence import RuntimeFence
from tests.helpers import GENERATION_A, GENERATION_B, RuntimeControlDB, runtime_control


@pytest.fixture
def guarded_app(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    db = RuntimeControlDB(runtime_control(generation=GENERATION_A))
    app.state.pjud_runtime_fence = RuntimeFence(db, GENERATION_A)
    app.state.session_pool = MagicMock(acquire=AsyncMock())
    app.state.private_resolution_budget = MagicMock()
    yield app, db
    get_settings.cache_clear()


AUTH = {"Authorization": "Bearer test-key", "x-pjud-runtime-generation": GENERATION_A}


@pytest.mark.parametrize("path,body", [
    ("/api/v1/search", {}), ("/api/v1/detail", {}), ("/api/v1/familia/sync", {}),
])
def test_paused_routes_reject_before_pool_and_private_budget(guarded_app, path, body):
    app, db = guarded_app
    db.control["admission_paused"] = True
    response = TestClient(app).post(path, json=body, headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "pjud_admission_paused"}
    app.state.session_pool.acquire.assert_not_called()
    assert app.state.private_resolution_budget.mock_calls == []
    if "familia" in path:
        assert response.headers["cache-control"] == "private, no-store"


def test_invalid_auth_never_reads_control(guarded_app):
    app, db = guarded_app
    response = TestClient(app).post("/api/v1/search", json={}, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert db.calls == []
    app.state.session_pool.acquire.assert_not_called()


@pytest.mark.parametrize("origin", [[], [("x-pjud-runtime-generation", GENERATION_B)],
    [("x-pjud-runtime-generation", GENERATION_A)] * 2,
    [("x-pjud-runtime-generation", GENERATION_A + ", " + GENERATION_A)]])
def test_strict_origin_header_must_be_single_current_value(guarded_app, origin):
    app, _ = guarded_app
    response = TestClient(app).post("/api/v1/search", json={}, headers=[("Authorization", "Bearer test-key"), *origin])
    assert response.status_code == 503
    assert response.json() == {"detail": "pjud_runtime_generation_mismatch"}


def test_health_readable_and_reopen_reaches_normal_validation(guarded_app):
    app, db = guarded_app
    db.control["admission_paused"] = True
    client = TestClient(app)
    assert client.get("/api/v1/health").status_code == 200
    assert db.calls == []
    db.control["admission_paused"] = False
    assert client.post("/api/v1/search", json={}, headers=AUTH).status_code == 422
    assert len(db.calls) == 1


def test_missing_runtime_state_fails_closed(guarded_app):
    app, _ = guarded_app
    del app.state.pjud_runtime_fence
    response = TestClient(app).post("/api/v1/search", json={}, headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "pjud_runtime_unavailable"}
