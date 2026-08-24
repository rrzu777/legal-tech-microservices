"""Tests for API rate limiting."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()


@pytest.fixture
def client():
    from app.main import create_app
    app = create_app()
    return TestClient(app)


AUTH = {"Authorization": "Bearer test-key"}


def _make_mock_pool():
    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()
    mock_session.search = AsyncMock(return_value="<html></html>")
    mock_session.detail = AsyncMock(return_value="<html></html>")
    mock_session.close = AsyncMock()
    mock_session.age_seconds = 0
    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_session)
    mock_pool.release = AsyncMock()
    mock_pool.close_all = AsyncMock()
    return mock_pool


class TestRateLimit:
    def test_penal_batch_is_one_rate_limited_api_operation(self, client, monkeypatch):
        from app.models import SearchResponse
        from app.routes import search as search_route

        run_batch = AsyncMock(return_value=SearchResponse(
            found=False, match_count=0, matches=[], blocked=False, error=None,
            case_type="rit", status="not_found",
        ))
        monkeypatch.setattr(search_route, "_run_penal_book_batch", run_batch)
        payload = {
            "contract_version": 2, "competencia": "penal", "case_type": "rit",
            "case_number": "E-77-2025", "books": ["1", "2", "3", "4", "5"],
            "tribunal_label": "7º Juzgado de Garantía",
            "caption": "MINISTERIO PÚBLICO / PERSONA E",
        }
        assert client.post("/api/v1/search/penal-books", json=payload, headers=AUTH).status_code == 200
        assert client.post("/api/v1/search/penal-books", json=payload, headers=AUTH).status_code == 429
        assert run_batch.await_count == 1

    def test_search_rate_limited_after_5_requests(self, client):
        """6th request within a minute should return 429."""
        client.app.state.session_pool = _make_mock_pool()
        payload = {"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"}

        for _ in range(5):
            resp = client.post("/api/v1/search", json=payload, headers=AUTH)
            assert resp.status_code == 200

        resp = client.post("/api/v1/search", json=payload, headers=AUTH)
        assert resp.status_code == 429

    def test_detail_rate_limited_after_5_requests(self, client):
        """6th detail request within a minute should return 429."""
        client.app.state.session_pool = _make_mock_pool()
        payload = {"detail_key": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJjb21wZXRlbmNpYSI6ImNpdmlsIn0.fake"}

        for _ in range(5):
            resp = client.post("/api/v1/detail", json=payload, headers=AUTH)
            assert resp.status_code == 200

        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)
        assert resp.status_code == 429

    def test_health_not_rate_limited(self, client):
        """Health endpoint should never be rate limited."""
        for _ in range(20):
            resp = client.get("/api/v1/health")
            assert resp.status_code == 200
