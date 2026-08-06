from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.catalogs import CatalogResult, CatalogService, parse_html_options, parse_json_options
from app.failure_kind import BlockedPageError
from app.session import OJVSession
from tests.helpers import AdapterQueGraba


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


def test_parse_html_options_rejects_non_option_html_and_keeps_utf8_labels():
    """Catches treating a challenge page as the books catalog."""
    assert parse_html_options("<html><body>Access denied</body></html>") == []
    assert parse_html_options(
        '<option value="0">Seleccione</option>'
        '<option value="31">Penal – Ñuble</option>'
    ) == [{"code": "31", "label": "Penal – Ñuble"}]


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


def test_catalog_routes_are_authenticated_and_publish_the_expected_schema(monkeypatch):
    """Catches catalog routes being public or drifting away from the client contract."""
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    spec = app.openapi()

    for path in [
        "/api/v1/catalogs/courts",
        "/api/v1/catalogs/tribunals",
        "/api/v1/catalogs/books",
    ]:
        operation = spec["paths"][path]["get"]
        assert operation["security"] == [{"HTTPBearer": []}]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["$ref"] == "#/components/schemas/CatalogResponse"

    client = TestClient(app)
    assert client.get("/api/v1/catalogs/courts").status_code == 401


def test_catalog_routes_reject_suprema_for_catalogs_it_never_uses(monkeypatch):
    """Catches issuing an impossible tribunal/books request for Suprema."""
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    result = CatalogResult(
        options=[{"code": "90", "label": "C.A. de Santiago"}],
        source="snapshot",
        fetched_at="2026-08-05T12:00:00+00:00",
    )
    app.state.catalog_service = MagicMock(
        tribunals=AsyncMock(return_value=result), books=AsyncMock(return_value=result)
    )
    client = TestClient(app, raise_server_exceptions=False)
    auth = {"Authorization": "Bearer test-key"}

    assert client.get(
        "/api/v1/catalogs/tribunals?competencia=suprema&corte=90", headers=auth
    ).status_code == 422
    assert client.get(
        "/api/v1/catalogs/books?competencia=suprema&anno=2025", headers=auth
    ).status_code == 422
