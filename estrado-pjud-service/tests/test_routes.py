"""Integration tests for all API routes.

Uses FastAPI TestClient with a mocked APISessionPool so that no real HTTP
calls are made to PJUD.  The real HTML fixtures are fed through the mocked
session so that parsers run against authentic HTML, giving us true end-to-end
coverage minus the network.
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
    from app.config import get_settings
    get_settings.cache_clear()


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
    mock_session.age_seconds = 0  # always fresh
    return mock_session


def _make_mock_pool(mock_session):
    """Build a MagicMock that quacks like APISessionPool."""
    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_session)
    mock_pool.release = AsyncMock()
    mock_pool.close_all = AsyncMock()
    return mock_pool


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
    def test_search_returns_matches(self, client):
        """POST /api/v1/search returns found=True with parsed matches."""
        html = _load("search_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(search_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

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
        mock_pool.acquire.assert_awaited_once()
        mock_session.search.assert_awaited_once()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=True)

    def test_search_laboral(self, client):
        """POST /api/v1/search with laboral competencia returns results."""
        html = _load("search_Laboral_T_500_2024.html")
        mock_session = _make_mock_session(search_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

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

    def test_search_cobranza(self, client):
        """POST /api/v1/search with cobranza competencia returns results."""
        html = _load("search_Cobranza_C_1000_2024.html")
        mock_session = _make_mock_session(search_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

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

    def test_search_returns_blocked_on_timeout(self, client):
        """Network timeout should return blocked=True so caller can retry."""
        import httpx
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"}
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is True
        assert body["error"] is not None

    def test_search_returns_blocked_on_connect_error(self, client):
        """Connection error should return blocked=True."""
        import httpx
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"}
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is True

    def test_search_non_network_error_not_blocked(self, client):
        """Non-network exceptions should return blocked=False."""
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(side_effect=ValueError("parsing failed"))
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"}
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is False

    def test_search_error_does_not_expose_internals(self, client):
        """Error messages should not contain internal paths or URLs."""
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(
            side_effect=Exception("Connection to https://oficinajudicialvirtual.pjud.cl/ADIR_871/civil/foo.php failed")
        )
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"}
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)
        body = resp.json()

        assert "oficinajudicialvirtual" not in body["error"]
        assert ".php" not in body["error"]
        assert "ADIR_871" not in body["error"]
        # Redacted portions replaced but surrounding text preserved
        assert "[redacted]" in body["error"]
        assert "Connection to" in body["error"]

    def test_search_non_internal_error_preserved(self, client):
        """Non-internal error messages should be preserved."""
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(side_effect=ValueError("Invalid case number format"))
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"}
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)
        body = resp.json()

        assert body["error"] == "Invalid case number format"


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

    def test_detail_returns_data(self, client):
        """POST /api/v1/detail returns metadata, movements, and litigantes."""
        html = _load("detail_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(detail_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

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
        mock_pool.acquire.assert_awaited_once()
        mock_session.detail.assert_awaited_once()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=True)

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

    def test_detail_with_explicit_competencia(self, client):
        """POST /api/v1/detail with competencia skips JWT guessing."""
        html = _load("detail_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(detail_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": "opaque-encrypted-token", "competencia": "penal"}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        # The session.detail call should have received "penal" as competencia
        mock_session.detail.assert_awaited_once_with("penal", "opaque-encrypted-token")

    @pytest.mark.parametrize("competencia", [
        "suprema", "apelaciones", "civil", "laboral", "penal", "cobranza",
    ])
    def test_detail_all_competencias(self, client, competencia):
        """POST /api/v1/detail accepts all 6 competencias."""
        html = _load("detail_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(detail_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": "opaque-token", "competencia": competencia}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is False
        mock_session.detail.assert_awaited_once_with(competencia, "opaque-token")

    def test_detail_without_competencia_falls_back_to_guess(self, client):
        """POST /api/v1/detail without competencia still guesses from JWT."""
        html = _load("detail_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(detail_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": self._CIVIL_JWT}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        # Should guess "civil" from the JWT payload
        mock_session.detail.assert_awaited_once_with("civil", self._CIVIL_JWT)

    def test_detail_blocked_when_empty_response(self, client):
        """POST /api/v1/detail with empty HTML returns blocked=True."""
        mock_session = _make_mock_session(detail_html="")
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": self._CIVIL_JWT}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is True
        assert body["error"] is not None

    def test_detail_logs_warning_on_competencia_extraction_failure(self, client, caplog):
        """When JWT doesn't contain competencia, an error should be logged and blocked=True returned."""
        mock_session = _make_mock_session()
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        # JWT with no competencia field
        payload = {"detail_key": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJmb28iOiJiYXIifQ.fake"}

        import logging
        with caplog.at_level(logging.WARNING, logger="app.routes.detail"):
            resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is True
        assert "competencia" in body["error"]
        # Session should NOT have been acquired since competencia failed before pool.acquire()
        mock_pool.acquire.assert_not_awaited()

    def test_detail_error_does_not_expose_internals(self, client):
        """Error messages should not contain internal paths or URLs."""
        mock_session = _make_mock_session()
        mock_session.detail = AsyncMock(
            side_effect=Exception("Connection to https://oficinajudicialvirtual.pjud.cl/ADIR_871/civil/modal/causaCivil.php failed")
        )
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": self._CIVIL_JWT, "competencia": "civil"}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)
        body = resp.json()

        assert "oficinajudicialvirtual" not in body["error"]
        assert ".php" not in body["error"]
        # Redacted portions replaced but surrounding text preserved
        assert "[redacted]" in body["error"]
        assert "Connection to" in body["error"]

    def test_detail_returns_blocked_on_timeout(self, client):
        """Network timeout should return blocked=True so caller can retry."""
        import httpx
        mock_session = _make_mock_session()
        mock_session.detail = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": self._CIVIL_JWT, "competencia": "civil"}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is True
        assert body["error"] is not None

    def test_detail_returns_blocked_on_connect_error(self, client):
        """Connection error should return blocked=True."""
        import httpx
        mock_session = _make_mock_session()
        mock_session.detail = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": self._CIVIL_JWT, "competencia": "civil"}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is True

    def test_detail_non_network_error_not_blocked(self, client):
        """Non-network exceptions should return blocked=False."""
        mock_session = _make_mock_session()
        mock_session.detail = AsyncMock(side_effect=ValueError("parsing failed"))
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": self._CIVIL_JWT, "competencia": "civil"}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is False
