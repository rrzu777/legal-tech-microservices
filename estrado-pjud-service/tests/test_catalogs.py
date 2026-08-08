from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.catalogs import (
    CatalogContentError,
    CatalogResult,
    CatalogService,
    parse_html_options,
    parse_json_options,
)
from app.failure_kind import BlockedPageError
from app.session import OJVSession
from tests.helpers import AdapterQueGraba
from scripts import refresh_catalog_snapshot as refresh


def test_snapshot_refresh_accepts_the_exact_current_17_court_codes():
    assert refresh.COURT_CODES == {
        "10", "11", "15", "20", "25", "30", "35", "40", "45", "46",
        "50", "55", "56", "60", "61", "90", "91",
    }


@pytest.mark.asyncio
async def test_fetch_with_session_never_acquires_pool_or_tracks_budget():
    session = MagicMock()
    session.catalog_json = AsyncMock(return_value=[{
        "COD_TRIBUNAL": "123",
        "GLS_TRIBUNAL": "2º Juzgado Civil de Santiago",
    }])
    pool = MagicMock()
    tracker = MagicMock()
    service = CatalogService(pool, snapshot={}, proxy_usage=tracker)

    options = await service.fetch_with_session(
        session,
        "tribunals",
        {"competencia": "civil", "corte": "90", "tipo_busqueda": "1"},
        retry_transport=False,
    )

    assert options == [{"code": "123", "label": "2º Juzgado Civil de Santiago"}]
    pool.acquire.assert_not_called()
    tracker.track.assert_not_called()
    session.catalog_json.assert_awaited_once_with(
        "/combosJSON/leeTrib.php",
        {"codCompetencia": "3", "codCorte": "90", "tipoBusqueda": "1"},
        retry_transport=False,
    )


@pytest.mark.asyncio
async def test_fetch_with_session_rejects_empty_options():
    session = MagicMock()
    session.catalog_html = AsyncMock(return_value="<option value='0'>Seleccione</option>")
    service = CatalogService(MagicMock(), snapshot={})

    with pytest.raises(CatalogContentError):
        await service.fetch_with_session(
            session,
            "books",
            {"competencia": "civil", "corte": "90", "anno": "2026"},
        )


@pytest.mark.asyncio
async def test_fetch_with_session_classifies_invalid_payload_as_catalog_content_error():
    session = MagicMock()
    session.catalog_json = AsyncMock(side_effect=ValueError("not JSON"))
    service = CatalogService(MagicMock(), snapshot={})

    with pytest.raises(CatalogContentError):
        await service.fetch_with_session(
            session,
            "tribunals",
            {"competencia": "civil", "corte": "90", "tipo_busqueda": "1"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [307, 308])
async def test_fetch_with_session_classifies_redirect_as_invalid_catalog(status):
    request = httpx.Request("POST", "https://x/catalog")
    response = httpx.Response(
        status,
        headers={"location": "/unexpected"},
        request=request,
    )
    session = MagicMock()
    session.catalog_json = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "redirect",
            request=request,
            response=response,
        )
    )
    service = CatalogService(MagicMock(), snapshot={})

    with pytest.raises(CatalogContentError):
        await service.fetch_with_session(
            session,
            "tribunals",
            {"competencia": "civil", "corte": "90", "tipo_busqueda": "1"},
            retry_transport=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 405, 429])
async def test_fetch_with_session_classifies_session_waf_status_as_invalid(status):
    request = httpx.Request("POST", "https://x/catalog")
    response = httpx.Response(status, request=request)
    session = MagicMock()
    session.catalog_json = AsyncMock(side_effect=httpx.HTTPStatusError(
        "session rejected",
        request=request,
        response=response,
    ))

    with pytest.raises(CatalogContentError):
        await CatalogService(MagicMock(), snapshot={}).fetch_with_session(
            session,
            "tribunals",
            {"competencia": "civil", "corte": "90", "tipo_busqueda": "1"},
            retry_transport=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 500, 503])
async def test_fetch_with_session_preserves_non_session_http_failures(status):
    request = httpx.Request("POST", "https://x/catalog")
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError("upstream", request=request, response=response)
    session = MagicMock()
    session.catalog_json = AsyncMock(side_effect=error)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await CatalogService(MagicMock(), snapshot={}).fetch_with_session(
            session,
            "tribunals",
            {"competencia": "civil", "corte": "90", "tipo_busqueda": "1"},
            retry_transport=False,
        )

    assert raised.value.response.status_code == status


def test_parse_json_options_discards_placeholder_duplicates_and_normalizes_text():
    """Catches a parser that would expose PJUD's "Seleccione" entry as a court."""
    raw = [
        {"COD_TRIBUNAL": "0", "GLS_TRIBUNAL": "Seleccione un tribunal"},
        {"COD_TRIBUNAL": "123", "GLS_TRIBUNAL": " 2º Juzgado "},
        {"COD_TRIBUNAL": "123", "GLS_TRIBUNAL": "2º Juzgado"},
    ]

    assert parse_json_options(raw, "COD_TRIBUNAL", "GLS_TRIBUNAL") == [
        {"code": "123", "label": "2º Juzgado"},
    ]


def test_parse_json_options_rejects_conflicting_duplicate_codes():
    with pytest.raises(CatalogContentError):
        parse_json_options([
            {"COD_TRIBUNAL": "123", "GLS_TRIBUNAL": "Alfa"},
            {"COD_TRIBUNAL": "123", "GLS_TRIBUNAL": "Zeta"},
        ], "COD_TRIBUNAL", "GLS_TRIBUNAL")


def test_parse_html_options_rejects_non_option_html_and_keeps_utf8_labels():
    """Catches treating a challenge page as the books catalog."""
    assert parse_html_options("<html><body>Access denied</body></html>") == []
    assert parse_html_options(
        '<option value="0">Seleccione</option>'
        '<option value="31">Penal – Ñuble</option>'
    ) == [{"code": "31", "label": "Penal – Ñuble"}]


@pytest.mark.parametrize("html", [
    '<!doctype html><body><option value="13">Metropolitana</option></body>',
    '<body><option value="13">Metropolitana</option></body>',
    '<form><option value="13">Metropolitana</option></form>',
    '<option value="13">Metropolitana</option>unexpected',
])
def test_parse_html_options_rejects_anything_but_a_top_level_option_fragment(html):
    """Catches WAF/login HTML that embeds a plausible-looking option tag."""
    assert parse_html_options(html) == []


@pytest.mark.asyncio
async def test_catalog_falls_back_to_snapshot_when_live_fails():
    """Catches a timeout making catalog-dependent registration unavailable."""
    service = CatalogService(
        MagicMock(),
        snapshot={
            "generated_at": "2026-08-05T12:00:00+00:00",
            "tribunals": {
                "civil:90:1": {
                    "fetched_at": "2026-08-05T12:00:00+00:00",
                    "options": [{"code": "123", "label": "2º Juzgado Civil de Santiago"}],
                }
            },
        },
    )
    service._fetch_live = AsyncMock(side_effect=TimeoutError())

    result = await service.tribunals("civil", 90, 1)

    assert result.source == "snapshot"
    assert result.fetched_at == "2026-08-05T12:00:00+00:00"
    assert result.options == [{"code": "123", "label": "2º Juzgado Civil de Santiago"}]


def test_loaded_snapshot_resolves_one_global_tribunal_identity_without_pool_io():
    """Broad worker lookup must use loaded official data, never a second slot."""
    pool = MagicMock()
    service = CatalogService(
        pool,
        snapshot={
            "generated_at": "2026-08-06T00:00:00+00:00",
            "tribunals": {
                "civil:90:1": {
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                    "options": [{"code": "321", "label": "2º Juzgado Civil de Santiago"}],
                },
                "civil:91:1": {
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                    "options": [{"code": "400", "label": "1º Juzgado Civil de San Miguel"}],
                },
            },
        },
    )

    identity = service.resolve_loaded_tribunal("civil", " 2° juzgado civil de santiago ")

    assert identity is not None
    assert (identity.court_code, identity.tribunal_code) == (90, 321)
    assert identity.tribunal_label == "2º Juzgado Civil de Santiago"
    pool.acquire.assert_not_called()


def test_loaded_snapshot_fails_closed_when_label_maps_to_two_courts():
    service = CatalogService(
        MagicMock(),
        snapshot={
            "generated_at": "2026-08-06T00:00:00+00:00",
            "tribunals": {
                "civil:90:1": {
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                    "options": [{"code": "321", "label": "Juzgado Duplicado"}],
                },
                "civil:91:1": {
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                    "options": [{"code": "400", "label": "Juzgado Duplicado"}],
                },
            },
        },
    )

    assert service.resolve_loaded_tribunal("civil", "juzgado duplicado") is None


def test_loaded_snapshot_resolves_one_court_without_pool_io():
    """HTTP search must resolve appeal rows locally while its only slot is busy."""
    pool = MagicMock()
    service = CatalogService(
        pool,
        snapshot={
            "generated_at": "2026-08-06T00:00:00+00:00",
            "courts": {
                "1": {
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                    "options": [
                        {"code": "90", "label": "C.A. de Santiago"},
                        {"code": "91", "label": "C.A. de San Miguel"},
                    ],
                }
            },
        },
    )

    assert service.resolve_loaded_court(" c.a. DE santiago ") == 90
    pool.acquire.assert_not_called()


def test_loaded_snapshot_resolves_canonical_book_label_for_request_context():
    """Catches inventing a Penal book label instead of using the official snapshot."""
    pool = MagicMock()
    service = CatalogService(
        pool,
        snapshot={
            "generated_at": "2026-08-06T00:00:00+00:00",
            "books": {
                "penal:90:2025": {
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                    "options": [
                        {"code": "1", "label": "Ordinaria"},
                        {"code": "2", "label": "Exhorto"},
                    ],
                }
            },
        },
    )

    assert service.resolve_loaded_book("penal", "1", 2025, corte=90) == {
        "code": "1",
        "label": "Ordinaria",
    }
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_uses_nonempty_live_value_for_24_hour_cache(monkeypatch):
    """Catches refetching every request instead of keeping the live catalog for its TTL."""
    service = CatalogService(MagicMock(), snapshot={"generated_at": "2026-08-05T12:00:00+00:00"})
    service._fetch_live = AsyncMock(return_value=[{"code": "90", "label": "C.A. de Santiago"}])

    first = await service.courts(1)
    second = await service.courts(1)

    assert first.source == "live"
    assert second.source == "cache"
    assert service._fetch_live.await_count == 1


@pytest.mark.asyncio
async def test_live_catalog_runs_inside_durable_proxy_usage_operation():
    session = MagicMock()
    session.catalog_json = AsyncMock(return_value=[{
        "COD_CORTE": "90", "GLS_CORTE": "C.A. de Santiago",
    }])
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=session)
    pool.release = AsyncMock()
    tracked = []

    class Tracker:
        def track(self, **kwargs):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def scope():
                tracked.append(kwargs)
                yield MagicMock()

            return scope()

    service = CatalogService(pool, snapshot={}, proxy_usage=Tracker())

    result = await service.courts()

    assert result.source == "live"
    assert tracked == [{"operation": "catalog"}]


@pytest.mark.asyncio
async def test_empty_live_value_is_not_cached_or_used_as_a_catalog():
    """Catches overwriting a healthy catalog with PJUD's empty/challenge response."""
    service = CatalogService(
        MagicMock(),
        snapshot={
            "generated_at": "2026-08-05T12:00:00+00:00",
            "courts": {
                "1": {
                    "fetched_at": "2026-08-05T12:00:00+00:00",
                    "options": [{"code": "90", "label": "C.A. de Santiago"}],
                }
            },
        },
    )
    service._fetch_live = AsyncMock(return_value=[])

    result = await service.courts(1)

    assert result.source == "snapshot"
    assert result.options == [{"code": "90", "label": "C.A. de Santiago"}]
    assert service._cache == {}


@pytest.mark.asyncio
async def test_wrapped_books_html_invalidates_the_session_and_uses_snapshot():
    """Catches caching a WAF/login page that embeds a valid-looking option."""
    session = MagicMock()
    session.catalog_html = AsyncMock(
        return_value='<!doctype html><body><option value="13">Metropolitana</option></body>'
    )
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=session)
    pool.release = AsyncMock()
    service = CatalogService(
        pool,
        snapshot={
            "generated_at": "2026-08-05T12:00:00+00:00",
            "books": {
                "civil:90:2025": {
                    "fetched_at": "2026-08-05T12:00:00+00:00",
                    "options": [{"code": "C", "label": "C"}],
                }
            },
        },
    )

    result = await service.books("civil", 90, 2025)

    assert result.source == "snapshot"
    assert service._cache == {}
    pool.release.assert_awaited_once_with(session, healthy=False)


@pytest.mark.asyncio
async def test_invalid_live_catalog_discards_its_session_before_snapshot_fallback():
    """Catches retrying a WAF-blocked combo through the same pooled session/IP."""
    session = MagicMock()
    session.catalog_json = AsyncMock(side_effect=BlockedPageError("F5 combo response"))
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=session)
    pool.release = AsyncMock()
    service = CatalogService(
        pool,
        snapshot={
            "generated_at": "2026-08-05T12:00:00+00:00",
            "courts": {
                "1": {
                    "fetched_at": "2026-08-05T12:00:00+00:00",
                    "options": [{"code": "90", "label": "C.A. de Santiago"}],
                }
            },
        },
    )

    result = await service.courts(1)

    assert result.source == "snapshot"
    pool.release.assert_awaited_once_with(session, healthy=False)


@pytest.mark.asyncio
async def test_catalog_json_rejects_an_f5_body_before_attempting_json_decode():
    """Catches an HTTP-200 WAF page being treated like a malformed but healthy combo."""
    session = OJVSession(AdapterQueGraba(cuerpo_post=b"<html>bobcmn</html>"))

    with pytest.raises(BlockedPageError):
        await session.catalog_json("/combosJSON/leeCorte.php", {"tipoBusqueda": "1"})


@pytest.mark.asyncio
async def test_catalog_cache_expires_after_24_hours(monkeypatch):
    """Catches a stale in-memory catalog being served past the declared 24-hour TTL."""
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    service = CatalogService(
        MagicMock(),
        snapshot={"generated_at": "2026-08-05T12:00:00+00:00"},
        now=lambda: now,
    )
    service._fetch_live = AsyncMock(
        side_effect=[
            [{"code": "90", "label": "C.A. de Santiago"}],
            [{"code": "91", "label": "C.A. de San Miguel"}],
        ]
    )

    await service.courts(1)
    monkeypatch.setattr(service, "_now", lambda: datetime(2026, 8, 6, 12, 0, 1, tzinfo=UTC))
    result = await service.courts(1)

    assert result.source == "live"
    assert result.options == [{"code": "91", "label": "C.A. de San Miguel"}]


def test_catalog_routes_are_not_exposed_by_the_local_only_runtime(monkeypatch):
    """No authenticated request may reactivate live catalog traffic."""
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    spec = app.openapi()

    catalog_paths = [
        "/api/v1/catalogs/courts",
        "/api/v1/catalogs/tribunals",
        "/api/v1/catalogs/books",
    ]
    assert all(path not in spec["paths"] for path in catalog_paths)

    client = TestClient(app)
    auth = {"Authorization": "Bearer test-key"}
    assert all(client.get(path, headers=auth).status_code == 404 for path in catalog_paths)
