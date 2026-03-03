"""Integration tests for all API routes.

Uses FastAPI TestClient with mocked OJVSession / OJVHttpAdapter
so that no real HTTP calls are made to PJUD.  The real HTML fixtures
are fed through the mocked session so that parsers run against
authentic HTML, giving us true end-to-end coverage minus the network.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    """Load a fixture file and decode as latin-1 (matches OJVSession behaviour)."""
    return (FIXTURES / name).read_bytes().decode("latin-1")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Ensure API_KEY is set for every test."""
    monkeypatch.setenv("API_KEY", "test-key")


@pytest.fixture
def client():
    """Create a fresh TestClient for each test.

    Importing inside the fixture ensures the env-var is already set
    (thanks to the autouse ``_env`` fixture).
    """
    from app.main import create_app

    app = create_app()
    return TestClient(app)


AUTH = {"Authorization": "Bearer test-key"}


def _make_mock_session(*, search_html: str | None = None, detail_html: str | None = None):
    """Build a MagicMock that quacks like OJVSession.

    Parameters
    ----------
    search_html:
        HTML string that ``session.search()`` will return.
    detail_html:
        HTML string that ``session.detail()`` will return.
    """
    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()
    mock_session.search = AsyncMock(return_value=search_html or "")
    mock_session.detail = AsyncMock(return_value=detail_html or "")
    mock_session.close = AsyncMock()
    return mock_session


# ===================================================================
# Health
# ===================================================================

class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "uptime_seconds" in body
        assert isinstance(body["uptime_seconds"], int)


# ===================================================================
# Search
# ===================================================================

class TestSearch:
    @patch("app.routes.search.OJVHttpAdapter")
    @patch("app.routes.search.OJVSession")
    def test_search_returns_matches(self, MockSession, MockAdapter, client):
        """POST /api/v1/search returns found=True with parsed matches."""
        html = _load("search_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(search_html=html)
        MockSession.return_value = mock_session

        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
        }
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["found"] is True
        assert body["match_count"] >= 1
        assert body["blocked"] is False
        assert body["error"] is None

        # Validate match structure
        first = body["matches"][0]
        assert first["key"].startswith("eyJ")
        assert first["rol"]
        assert first["tribunal"]
        assert first["caratulado"]
        assert "fecha_ingreso" in first

        # Verify the mock was used correctly
        mock_session.initialize.assert_awaited_once()
        mock_session.search.assert_awaited_once()
        mock_session.close.assert_awaited_once()

    @patch("app.routes.search.OJVHttpAdapter")
    @patch("app.routes.search.OJVSession")
    def test_search_laboral(self, MockSession, MockAdapter, client):
        """POST /api/v1/search with laboral competencia returns results."""
        html = _load("search_Laboral_T_500_2024.html")
        mock_session = _make_mock_session(search_html=html)
        MockSession.return_value = mock_session

        payload = {
            "case_type": "rit",
            "case_number": "T-500-2024",
            "competencia": "laboral",
        }
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["found"] is True
        assert body["match_count"] >= 1

    @patch("app.routes.search.OJVHttpAdapter")
    @patch("app.routes.search.OJVSession")
    def test_search_cobranza(self, MockSession, MockAdapter, client):
        """POST /api/v1/search with cobranza competencia returns results."""
        html = _load("search_Cobranza_C_1000_2024.html")
        mock_session = _make_mock_session(search_html=html)
        MockSession.return_value = mock_session

        payload = {
            "case_type": "rol",
            "case_number": "C-1000-2024",
            "competencia": "cobranza",
        }
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["found"] is True
        assert body["match_count"] >= 1

    def test_search_requires_auth(self, client):
        """POST /api/v1/search without Authorization header returns 401."""
        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
        }
        resp = client.post("/api/v1/search", json=payload)
        assert resp.status_code == 401

    def test_search_rejects_bad_key(self, client):
        """POST /api/v1/search with wrong API key returns 401."""
        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
        }
        bad_auth = {"Authorization": "Bearer wrong-key"}
        resp = client.post("/api/v1/search", json=payload, headers=bad_auth)
        assert resp.status_code == 401


# ===================================================================
# Detail
# ===================================================================

class TestDetail:
    # A minimal JWT-shaped string whose payload decodes to {"competencia": "civil"}
    # base64url('{"typ":"JWT","alg":"HS256"}') . base64url('{"competencia":"civil"}') . sig
    _CIVIL_JWT = (
        "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9"
        ".eyJjb21wZXRlbmNpYSI6ImNpdmlsIn0"
        ".fake_signature"
    )

    @patch("app.routes.detail.OJVHttpAdapter")
    @patch("app.routes.detail.OJVSession")
    def test_detail_returns_data(self, MockSession, MockAdapter, client):
        """POST /api/v1/detail returns metadata, movements, and litigantes."""
        html = _load("detail_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(detail_html=html)
        MockSession.return_value = mock_session

        payload = {"detail_key": self._CIVIL_JWT}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is False
        assert body["error"] is None

        # Metadata
        md = body["metadata"]
        assert md["rol"]
        assert "C-1234-2024" in md["rol"]
        assert md["tribunal"]
        assert md["estado_administrativo"]

        # Movements
        assert isinstance(body["movements"], list)
        assert len(body["movements"]) >= 1
        mov = body["movements"][0]
        assert "folio" in mov
        assert "tramite" in mov
        assert "descripcion" in mov

        # Litigantes
        assert isinstance(body["litigantes"], list)
        assert len(body["litigantes"]) >= 1
        lig = body["litigantes"][0]
        assert "rol" in lig
        assert "rut" in lig
        assert "nombre" in lig

        # Verify mock usage
        mock_session.initialize.assert_awaited_once()
        mock_session.detail.assert_awaited_once()
        mock_session.close.assert_awaited_once()

    def test_detail_requires_auth(self, client):
        """POST /api/v1/detail without Authorization header returns 401."""
        payload = {"detail_key": self._CIVIL_JWT}
        resp = client.post("/api/v1/detail", json=payload)
        assert resp.status_code == 401

    def test_detail_rejects_bad_key(self, client):
        """POST /api/v1/detail with wrong API key returns 401."""
        payload = {"detail_key": self._CIVIL_JWT}
        bad_auth = {"Authorization": "Bearer wrong-key"}
        resp = client.post("/api/v1/detail", json=payload, headers=bad_auth)
        assert resp.status_code == 401

    @patch("app.routes.detail.OJVHttpAdapter")
    @patch("app.routes.detail.OJVSession")
    def test_detail_blocked_when_empty_response(self, MockSession, MockAdapter, client):
        """POST /api/v1/detail with empty HTML returns blocked=True."""
        mock_session = _make_mock_session(detail_html="")
        MockSession.return_value = mock_session

        payload = {"detail_key": self._CIVIL_JWT}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is True
        assert body["error"] is not None
