"""Un 405 vacío en detalle significa sesión OJV rechazada, no causa inválida."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.failure_kind import (
    RejectedDetailSessionError,
    classify_exception,
    slot_still_healthy,
)
from app.session import OJVSession
from tests.helpers import AdapterQueGraba
from tests.test_engine import (
    _configure_sync_run_rpc,
    _make_case,
    _mock_search_response,
)


class _Adapter405(AdapterQueGraba):
    async def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        return httpx.Response(
            405,
            content=b"",
            request=httpx.Request("POST", f"https://ojv.test{path}"),
        )


async def test_detail_405_vacio_es_sesion_rechazada():
    session = OJVSession(_Adapter405())
    session.csrf_token = "a" * 32

    with pytest.raises(RejectedDetailSessionError):
        await session.detail("civil", "jwt.de.mentira")


def test_sesion_rechazada_es_infra_y_fuerza_remint():
    exc = RejectedDetailSessionError("405 vacío")
    assert classify_exception(exc) == "infra"
    assert slot_still_healthy(exc) is False


@pytest.mark.asyncio
async def test_worker_no_penaliza_causa_y_descarta_slot_rechazado():
    from worker.engine import SyncEngine

    mock_session = AsyncMock()
    mock_pool = AsyncMock()
    mock_pool.acquire = AsyncMock(return_value=mock_session)
    mock_pool.release = AsyncMock()
    mock_pool.enforce_global_rate_limit = AsyncMock()

    mock_sb = MagicMock()
    chain = MagicMock()
    mock_sb.from_.return_value = chain
    for method in ("insert", "select", "single", "update", "eq"):
        getattr(chain, method).return_value = chain
    chain.execute.return_value = MagicMock(data={"id": "sync-run-1"}, count=0)
    _configure_sync_run_rpc(mock_sb)

    engine = SyncEngine(
        pool=mock_pool,
        supabase=mock_sb,
        notifier=AsyncMock(),
        metrics=MagicMock(),
        backoff=MagicMock(),
        config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
    )

    with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
         patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail, \
         patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
        mock_search.return_value = _mock_search_response()
        mock_detail.side_effect = RejectedDetailSessionError("405 vacío")
        result = await engine.sync_case(_make_case())

    assert result == {"success": False, "new_movements": 0}
    mock_pool.release.assert_awaited_once_with(
        mock_session, disposition="replace_before_reuse",
    )
    mock_update_error.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_no_expone_fragmento_del_token(caplog):
    token = "0123456789abcdef0123456789abcdef"
    adapter = AdapterQueGraba(html_get=f"<script>token: '{token}'</script>")
    session = OJVSession(adapter)

    with caplog.at_level(logging.INFO):
        await session.initialize()

    assert session.csrf_token == token
    assert token not in caplog.text
    assert token[:8] not in caplog.text
    assert "CSRF token acquired" in caplog.text
