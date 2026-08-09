"""Test document download integration in sync engine."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_engine import (
    _make_case,
    _make_engine,
    _mock_detail_response,
    _mock_search_response,
)
from tests.helpers import find_update_payload


def test_r2_disabled_skips_documents():
    """When R2_ENABLED=False, no document processing happens."""
    from worker.config import WorkerConfig
    import os
    os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
    os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
    config = WorkerConfig(R2_ENABLED=False)
    assert config.R2_ENABLED is False


def test_r2_key_format():
    """R2 keys follow the expected path structure."""
    law_firm_id = "abc-123"
    case_id = "def-456"
    ext_key = "C-1234-2024:Principal:1"
    ext = "pdf"
    key = f"{law_firm_id}/{case_id}/{ext_key}.{ext}"
    assert key == "abc-123/def-456/C-1234-2024:Principal:1.pdf"


@pytest.mark.asyncio
async def test_normal_sync_never_downloads_documents_even_when_r2_exists():
    engine, *_ = _make_engine()
    engine._r2 = MagicMock()
    engine._download_and_store_documents = AsyncMock()
    detail = _mock_detail_response()
    detail["movements"][0].update({
        "documento_url": "/documento/primary",
        "documento_token": "secret-jwt",
    })

    with patch("worker.engine.search_pjud_via_session", new=AsyncMock(
        return_value=_mock_search_response(),
    )), patch("worker.engine.detail_pjud_via_session", new=AsyncMock(
        return_value=detail,
    )):
        result = await engine.sync_case(_make_case())

    assert result["success"] is True
    engine._download_and_store_documents.assert_not_awaited()


@pytest.mark.asyncio
async def test_movement_upsert_sanitizes_credentials_and_persists_sources():
    engine, _, supabase, *_ = _make_engine()
    chain = supabase.from_.return_value
    chain.execute.side_effect = [
        MagicMock(data=[]),
        MagicMock(data=[], count=0),
        MagicMock(data=[{
            "id": "movement-uuid-1",
            "external_movement_key": "C-1234-2024:Principal:1",
        }]),
        MagicMock(data=[], count=1),
        MagicMock(data=1),
    ]
    movement = {
        "folio": 1,
        "cuaderno": "Principal",
        "etapa": "Discusión",
        "tramite": "Resolución",
        "descripcion": "Provee demanda",
        "fecha": "2024-06-15",
        "foja": None,
        "documento_url": "/documento/primary",
        "documento_token": "secret-jwt",
        "documento_param": "dtaDoc",
    }

    new_count = await engine._upsert_movements(
        _make_case(),
        {"movements": [movement]},
    )

    assert new_count == 1
    row = chain.upsert.call_args.args[0][0]
    assert row["document_url"] is None
    assert row["has_remote_document"] is True
    assert row["raw_payload"] == {
        "folio": 1,
        "cuaderno": "Principal",
        "etapa": "Discusión",
        "tramite": "Resolución",
        "descripcion": "Provee demanda",
        "fecha": "2024-06-15",
        "foja": None,
    }
    supabase.rpc.assert_any_call("upsert_pjud_document_sources", {
        "p_law_firm_id": "firm-uuid-1",
        "p_case_id": "case-uuid-1",
        "p_movement_id": "movement-uuid-1",
        "p_sources": [{
            "document_kind": "principal",
            "source_id": "43dede8dd697bf6231f393b75700fd3e0ded96e9ecdc78fa8e9779c063070bfd",
            "ordinal": 0,
            "label": "Documento principal",
            "available": True,
        }],
    })


@pytest.mark.asyncio
async def test_source_registry_failure_does_not_fail_movement_sync():
    engine, _, supabase, *_ = _make_engine()
    chain = supabase.from_.return_value
    chain.execute.side_effect = [
        MagicMock(data=[]),
        MagicMock(data=[], count=0),
        MagicMock(data=[{
            "id": "movement-uuid-1",
            "external_movement_key": "C-1234-2024:Principal:1",
        }]),
        MagicMock(data=[], count=1),
        RuntimeError("migration not deployed"),
    ]
    movement = {
        "folio": 1,
        "cuaderno": "Principal",
        "tramite": "Resolución",
        "descripcion": "Provee",
        "fecha": "2024-06-15",
        "documento_url": "/documento/primary",
        "documento_token": "secret-jwt",
    }

    assert await engine._upsert_movements(
        _make_case(), {"movements": [movement]},
    ) == 1


@pytest.mark.asyncio
async def test_case_external_payload_is_recursively_sanitized():
    engine, _, supabase, *_ = _make_engine()
    detail = _mock_detail_response()
    detail["metadata"]["ebook_token"] = "secret"
    detail["metadata"]["nested"] = {
        "safe": "value",
        "document_url": "/documento/secret",
    }

    with patch("worker.engine.search_pjud_via_session", new=AsyncMock(
        return_value=_mock_search_response(),
    )), patch("worker.engine.detail_pjud_via_session", new=AsyncMock(
        return_value=detail,
    )):
        result = await engine.sync_case(_make_case())

    assert result["success"] is True
    payload = find_update_payload(supabase, last_sync_status="success")["external_payload"]
    assert payload["metadata"]["nested"] == {"safe": "value"}
    assert "ebook_token" not in payload["metadata"]
