"""Integration tests for all API routes.

Uses FastAPI TestClient with a mocked APISessionPool so that no real HTTP
calls are made to PJUD.  The real HTML fixtures are fed through the mocked
session so that parsers run against authentic HTML, giving us true end-to-end
coverage minus the network.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import http_status_error, infra_exceptions

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


@pytest.fixture
def client_5xx():
    """TestClient que DEVUELVE el 500 en vez de re-lanzar la excepción.

    Necesario para los tests de fallas de infra: el contrato que importa no es
    "la corrutina lanzó", es "la app recibe un 5xx y por eso la clasifica como
    NUESTRA y no le suma una falla a la causa". Con el cliente por defecto la
    excepción sube al test y el status code —que es lo que se está
    verificando— nunca se materializa.
    """
    from app.main import create_app

    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


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

    def test_health_reporta_down_cuando_el_pool_no_entrega_sesion(self, client):
        """El status sale DERIVADO y no de la constante `"ok"` que había acá.

        El 31 de julio de 2026 `/api/v1/health` respondía
        `{"status": "ok", "total_requests": 0, "total_errors": 0}` con la
        instancia llevando 3 días y 18 horas devolviendo 500 a todo. Un watchdog
        externo mira `.status` antes que cualquier contador, así que ese literal
        era lo que hacía indistinguible un servicio caído de uno ocioso.
        """
        from app.metrics import api_metrics
        api_metrics.record_pool_failure("search")

        body = client.get("/api/v1/health").json()

        assert body["status"] == "down"
        # Y la métrica de Familia se publica de verdad: `HealthResponse` se arma
        # con `**snapshot` y pydantic ignora en silencio lo que no declara.
        assert "familia_requests" in body


# ===================================================================
# Search
# ===================================================================

class TestSearch:
    @pytest.mark.asyncio
    async def test_court_label_resolution_requires_one_official_catalog_code(self):
        from app.catalogs import CatalogResult
        from app.routes.search import resolve_corte_code

        catalog_service = MagicMock()
        catalog_service.courts = AsyncMock(return_value=CatalogResult(
            options=[
                {"code": "91", "label": "C.A. de San Miguel"},
                {"code": "90", "label": "C.A. de San Miguel"},
            ],
            source="snapshot",
            fetched_at="2026-08-05T00:00:00+00:00",
        ))

        code = await resolve_corte_code(catalog_service, " C.A. DE SAN MIGUEL ")

        assert code is None

    def test_v2_appeals_search_resolves_result_court_once_for_ranking(self, client):
        from app.catalogs import CatalogResult
        from app.routes import search as search_route

        mock_session = _make_mock_session(search_html="<html>resultados</html>")
        client.app.state.session_pool = _make_mock_pool(mock_session)
        catalog_service = MagicMock()
        catalog_service.courts = AsyncMock(return_value=CatalogResult(
            options=[{"code": "91", "label": "C.A. de San Miguel"}],
            source="snapshot",
            fetched_at="2026-08-05T00:00:00+00:00",
        ))
        client.app.state.catalog_service = catalog_service
        raw_match = [{
            "key": "fresh-jwt",
            "rol": "Protección-4490-2025",
            "tribunal": "Corte apelaciones",
            "corte": "C.A. de San Miguel",
            "libro": "Protección",
            "libro_code": "34",
            "caratulado": "Parte",
            "fecha_ingreso": "2025-01-01",
        }]

        with patch.object(search_route, "parse_search_results", return_value=raw_match):
            response = client.post(
                "/api/v1/search",
                json={
                    "contract_version": 2,
                    "case_type": "rol",
                    "case_number": "4490-2025",
                    "competencia": "apelaciones",
                    "corte": 91,
                    "libro": "34",
                    "search_mode": "appeals_resource",
                },
                headers=AUTH,
            )

        assert response.status_code == 200
        assert response.json()["matches"][0]["corte_code"] == 91
        catalog_service.courts.assert_awaited_once_with()

    def test_v2_search_parser_value_error_is_upstream_changed(self, client):
        """Markup parser drift is not a missing case or an internal 5xx."""
        from app.routes import search as search_route

        mock_session = _make_mock_session(search_html="<html>nuevo markup</html>")
        client.app.state.session_pool = _make_mock_pool(mock_session)
        with patch.object(search_route, "parse_search_results", side_effect=ValueError("unexpected table")):
            response = client.post(
                "/api/v1/search",
                json={
                    "contract_version": 2,
                    "case_type": "rol",
                    "case_number": "C-1234-2024",
                    "competencia": "civil",
                    "corte": 90,
                    "tribunal": 321,
                },
                headers=AUTH,
            )

        assert response.status_code == 200
        assert response.json()["status"] == "upstream_changed"

    @pytest.mark.asyncio
    async def test_tribunal_label_resolution_requires_one_official_catalog_code(self):
        from app.catalogs import CatalogResult
        from app.models import SearchRequest
        from app.routes.search import resolve_tribunal_code

        request = SearchRequest(
            contract_version=2,
            case_type="rol",
            case_number="C-1234-2024",
            competencia="civil",
            corte=90,
            tribunal=321,
        )
        catalog_service = MagicMock()
        catalog_service.tribunals = AsyncMock(return_value=CatalogResult(
            options=[
                {"code": "321", "label": "2º Juzgado Civil de Santiago"},
                {"code": "999", "label": "2º Juzgado Civil de Santiago"},
            ],
            source="snapshot",
            fetched_at="2026-08-05T00:00:00+00:00",
        ))

        code = await resolve_tribunal_code(
            catalog_service, request, "  2º JUZGADO CIVIL DE SANTIAGO  "
        )

        assert code is None

    def test_v2_search_threads_canonical_fields_and_caps_ranked_matches(self, client):
        """v2 sends the canonical form once and exposes only its requested page."""
        from app.catalogs import CatalogResult
        from app.routes import search as search_route

        raw_matches = [
            {
                "key": f"key-{index:03d}",
                "rol": "C-1234-2024",
                "tribunal": f"{index + 1}º Juzgado Civil de Santiago",
                "caratulado": f"Parte {index}",
                "fecha_ingreso": "2024-01-01",
            }
            for index in range(11)
        ]
        mock_session = _make_mock_session(search_html="<html>resultados</html>")
        client.app.state.session_pool = _make_mock_pool(mock_session)
        catalog_service = MagicMock()
        catalog_service.tribunals = AsyncMock(return_value=CatalogResult(
            options=[
                {"code": str(321 if index == 0 else 400 + index), "label": f"{index + 1}º Juzgado Civil de Santiago"}
                for index in range(11)
            ],
            source="snapshot",
            fetched_at="2026-08-05T00:00:00+00:00",
        ))
        client.app.state.catalog_service = catalog_service

        with patch.object(search_route, "parse_search_results", return_value=raw_matches):
            response = client.post(
                "/api/v1/search",
                json={
                    "contract_version": 2,
                    "case_type": "rol",
                    "case_number": "C-1234-2024",
                    "competencia": "civil",
                    "corte": 90,
                    "tribunal": 321,
                    "max_matches": 10,
                },
                headers=AUTH,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "needs_disambiguation"
        assert body["match_count"] == 11
        assert len(body["matches"]) == 10
        assert body["truncated"] is True
        assert body["matches"][0]["tribunal_code"] == 321
        catalog_service.tribunals.assert_awaited_once_with("civil", 90)
        form = mock_session.search.call_args.args[1]
        assert form["conCorte"] == "90"
        assert form["conTribunal"] == "321"

    def test_v2_search_marks_unparseable_non_empty_upstream_response(self, client):
        """PJUD markup drift must not be reported as a missing case."""
        from app.routes import search as search_route

        mock_session = _make_mock_session(search_html="<html>nuevo markup PJUD</html>")
        client.app.state.session_pool = _make_mock_pool(mock_session)
        with patch.object(search_route, "parse_search_results", return_value=[]):
            response = client.post(
                "/api/v1/search",
                json={
                    "contract_version": 2,
                    "case_type": "rol",
                    "case_number": "C-1234-2024",
                    "competencia": "civil",
                    "corte": 90,
                    "tribunal": 321,
                },
                headers=AUTH,
            )

        assert response.status_code == 200
        assert response.json()["status"] == "upstream_changed"
        assert response.json()["found"] is False

    def test_v2_search_keeps_valid_results_when_optional_catalog_resolution_fails(self, client):
        """A ranking hint must never turn a parsed PJUD result into not-found."""
        from app.routes import search as search_route

        mock_session = _make_mock_session(search_html="<html>resultados</html>")
        client.app.state.session_pool = _make_mock_pool(mock_session)
        catalog_service = MagicMock()
        catalog_service.tribunals = AsyncMock(side_effect=RuntimeError("catalog unavailable"))
        client.app.state.catalog_service = catalog_service
        raw_match = [{
            "key": "key-1",
            "rol": "C-1234-2024",
            "tribunal": "2º Juzgado Civil de Santiago",
            "caratulado": "Parte",
            "fecha_ingreso": "2024-01-01",
        }]

        with patch.object(search_route, "parse_search_results", return_value=raw_match):
            response = client.post(
                "/api/v1/search",
                json={
                    "contract_version": 2,
                    "case_type": "rol",
                    "case_number": "C-1234-2024",
                    "competencia": "civil",
                    "corte": 90,
                    "tribunal": 321,
                },
                headers=AUTH,
            )

        assert response.status_code == 200
        assert response.json()["status"] == "found"
        assert response.json()["matches"][0]["tribunal_code"] is None

    def test_v2_search_classifies_read_timeout_as_pjud_timeout(self, client):
        """A portal timeout remains recoverable and is not a v2 not-found result."""
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(side_effect=httpx.ReadTimeout("OJV timed out"))
        client.app.state.session_pool = _make_mock_pool(mock_session)

        response = client.post(
            "/api/v1/search",
            json={
                "contract_version": 2,
                "case_type": "rol",
                "case_number": "C-1234-2024",
                "competencia": "civil",
                "corte": 90,
                "tribunal": 321,
            },
            headers=AUTH,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pjud_timeout"
        assert body["found"] is False
        assert body["blocked"] is False

    def test_v2_search_classifies_waf_as_pjud_blocked(self, client):
        mock_session = _make_mock_session(search_html='<script>window["bobcmn"] = "1"</script>')
        client.app.state.session_pool = _make_mock_pool(mock_session)

        response = client.post(
            "/api/v1/search",
            json={
                "contract_version": 2,
                "case_type": "rol",
                "case_number": "C-1234-2024",
                "competencia": "civil",
                "corte": 90,
                "tribunal": 321,
            },
            headers=AUTH,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pjud_blocked"
        assert body["blocked"] is True

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

    @pytest.mark.parametrize("exc", infra_exceptions(), ids=lambda e: type(e).__name__)
    def test_search_infra_devuelve_5xx_y_no_200(self, client_5xx, exc):
        """Una falla NUESTRA no puede salir con 200.

        Con 200 la app veía `found=False` y le escribía al abogado "No encontrada
        en OJV — revisa el rol y el tribunal" por una causa que existe y un proxy
        que estaba caído. El 5xx es lo único que la hace clasificar infra.
        """
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(side_effect=exc)
        client_5xx.app.state.session_pool = _make_mock_pool(mock_session)

        payload = {"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"}
        resp = client_5xx.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code >= 500

    def test_search_5xx_de_ojv_si_es_bloqueo(self, client):
        """Un 5xx del portal SÍ sale con 200 y `blocked`: es de ellos, no nuestro.

        La otra mitad de la simetría. Antes caía en `blocked=False` y la app lo
        contaba como falla de la causa; ahora no penaliza, pero tampoco se
        disfraza de caída nuestra.
        """
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(
            side_effect=http_status_error(503)
        )
        client.app.state.session_pool = _make_mock_pool(mock_session)

        payload = {"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"}
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json()["blocked"] is True

    def test_search_cuerpo_vacio_es_infra_no_causa_inexistente(self, client_5xx):
        """Cero bytes es el túnel cortándose, no "esa causa no existe".

        Sin esto `parse_search_results("")` devuelve [] y la respuesta sale
        `found=False` con 200 — indistinguible de un rol mal tipeado.
        """
        mock_session = _make_mock_session(search_html="   ")
        client_5xx.app.state.session_pool = _make_mock_pool(mock_session)

        payload = {"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"}
        resp = client_5xx.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code >= 500

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

    def test_search_with_libro_passes_to_form_data(self, client):
        """libro field is threaded through to build_search_form_data."""
        html = _load("search_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(search_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
            "libro": "V",
        }
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["found"] is True
        assert body["libro_used"] == "V"

        # Verify the form data sent to PJUD contained libro override
        call_args = mock_session.search.call_args
        form_data = call_args[0][1]  # second positional arg
        assert form_data["conTipoCausa"] == "V"

    def test_search_without_libro_echoes_tipo(self, client):
        """Without libro, libro_used echoes the tipo extracted from case_number."""
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
        assert body["libro_used"] == "C"

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
    def test_detail_affinity_explicit_not_found_is_valid_409_and_keeps_session_healthy(self, client):
        mock_session = _make_mock_session(
            search_html="<div>No se encontraron causas</div>",
            detail_html=_load("detail_Civil_C_1234_2024.html"),
        )
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        response = client.post(
            "/api/v1/detail",
            json={
                "detail_key": "caller-jwt-without-data",
                "competencia": "civil",
                "case_type": "rol",
                "case_number": "C-1234-2024",
            },
            headers=AUTH,
        )

        assert response.status_code == 409
        mock_session.detail.assert_not_awaited()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=True)

    @pytest.mark.parametrize("search_html", [
        " ",
        '<html><script>window["bobcmn"] = "1"</script></html>',
        "<html>markup PJUD desconocido</html>",
    ], ids=["empty", "waf", "parse_drift"])
    def test_detail_affinity_upstream_failures_are_not_409_or_detail_fetches(
        self, client_5xx, search_html
    ):
        """Only a valid but uncorrelatable search may end in affinity 409."""
        mock_session = _make_mock_session(
            search_html=search_html,
            detail_html=_load("detail_Civil_C_1234_2024.html"),
        )
        mock_pool = _make_mock_pool(mock_session)
        client_5xx.app.state.session_pool = mock_pool

        response = client_5xx.post(
            "/api/v1/detail",
            json={
                "detail_key": "caller-jwt-without-data",
                "competencia": "civil",
                "case_type": "rol",
                "case_number": "C-1234-2024",
            },
            headers=AUTH,
        )

        assert response.status_code >= 500
        assert response.status_code != 409
        mock_session.detail.assert_not_awaited()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=False)

    @pytest.mark.asyncio
    async def test_detail_affinity_accepts_unique_appeals_resource_with_book_prefix(self):
        from app.models import DetailRequest
        from app.routes import detail as detail_route

        request = DetailRequest(
            detail_key="caller-jwt-without-data",
            contract_version=2,
            case_type="rol",
            case_number="4490-2025",
            competencia="apelaciones",
            corte=90,
            libro="34",
            search_mode="appeals_resource",
        )
        session = _make_mock_session(search_html="<html>resultados</html>")
        with patch.object(detail_route, "parse_search_results", return_value=[{
            "key": "fresh-appeals-jwt",
            "rol": "Protección-4490-2025",
            "tribunal": "C.A. de Santiago",
            "caratulado": "Parte",
            "fecha_ingreso": "2025-01-01",
        }]):
            fresh_key = await detail_route._search_for_fresh_jwt(session, "apelaciones", request)

        assert fresh_key == "fresh-appeals-jwt"

    def test_detail_rejects_uncorrelated_fresh_jwt_instead_of_using_caller_token(self, client):
        """A fresh search must prove it found the requested detail candidate."""
        from app.routes import detail as detail_route

        mock_session = _make_mock_session(search_html="<html>resultados</html>", detail_html=_load("detail_Civil_C_1234_2024.html"))
        client.app.state.session_pool = _make_mock_pool(mock_session)
        with patch.object(detail_route, "parse_search_results", return_value=[{
            "key": "unrelated-jwt",
            "rol": "C-9999-2024",
            "tribunal": "1º Juzgado Civil",
            "caratulado": "Otra causa",
            "fecha_ingreso": "2024-01-01",
        }]):
            response = client.post(
                "/api/v1/detail",
                json={
                    "detail_key": "caller-jwt-without-data",
                    "competencia": "civil",
                    "case_type": "rol",
                    "case_number": "C-1234-2024",
                },
                headers=AUTH,
            )

        assert response.status_code == 409
        mock_session.detail.assert_not_awaited()
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

    def test_detail_cuerpo_vacio_es_infra(self, client_5xx):
        """Cero bytes sale 5xx: es el túnel cortándose, no un bloqueo de OJV."""
        mock_session = _make_mock_session(detail_html="")
        client_5xx.app.state.session_pool = _make_mock_pool(mock_session)

        payload = {"detail_key": self._CIVIL_JWT}
        resp = client_5xx.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code >= 500

    def test_detail_reconoce_el_challenge_f5_completo(self, client):
        """Una pagina de challenge de varios KB tambien es un bloqueo.

        Esta ruta miraba SOLO el largo, asi que el challenge F5 completo pasaba
        de largo: `parse_detail` no encontraba nada y la respuesta salia 200 con
        `blocked=False` y la causa vacia. El abogado veia un expediente sin
        movimientos en vez de un aviso de bloqueo. El worker tenia el test de
        esto (`test_detail_detects_f5_challenge`) y la ruta HTTP no — otra vez
        los dos servicios sin ser espejos.
        """
        challenge = (
            '<html><head><script>window["bobcmn"] = "10111...";</script></head><body>'
            + ("x" * 500)
            + "</body></html>"
        )
        assert len(challenge) > 100
        mock_session = _make_mock_session(detail_html=challenge)
        client.app.state.session_pool = _make_mock_pool(mock_session)

        payload = {"detail_key": self._CIVIL_JWT}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json()["blocked"] is True

    def test_detail_pagina_contentless_sigue_siendo_bloqueo(self, client):
        """Una página de ~39 bytes SÍ es soft-block de F5: 200 con blocked.

        La distinción con el test de arriba es fina y deliberada. Está medida:
        el F5 devuelve un esqueleto HTML vacío, no cero bytes. Colapsar las dos
        en "infra" perdería una señal real de bloqueo.
        """
        mock_session = _make_mock_session(
            detail_html="<html><head></head><body></body></html>",
        )
        client.app.state.session_pool = _make_mock_pool(mock_session)

        payload = {"detail_key": self._CIVIL_JWT}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json()["blocked"] is True

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

    @pytest.mark.parametrize("exc", infra_exceptions(), ids=lambda e: type(e).__name__)
    def test_detail_infra_devuelve_5xx_y_no_200(self, client_5xx, exc):
        """Espejo del de `search`: una falla nuestra no puede salir con 200."""
        mock_session = _make_mock_session()
        mock_session.detail = AsyncMock(side_effect=exc)
        client_5xx.app.state.session_pool = _make_mock_pool(mock_session)

        payload = {"detail_key": self._CIVIL_JWT, "competencia": "civil"}
        resp = client_5xx.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code >= 500

    def test_detail_5xx_de_ojv_si_es_bloqueo(self, client):
        mock_session = _make_mock_session()
        mock_session.detail = AsyncMock(
            side_effect=http_status_error(503)
        )
        client.app.state.session_pool = _make_mock_pool(mock_session)

        payload = {"detail_key": self._CIVIL_JWT, "competencia": "civil"}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json()["blocked"] is True

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

    def test_detail_includes_libro_field(self, client):
        """DetailResponse includes libro extracted from metadata."""
        html = _load("detail_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(detail_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": self._CIVIL_JWT}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["libro"] == "C"  # extracted from ROL "C-1234-2024"
        assert body["metadata"]["libro"] == "C"
