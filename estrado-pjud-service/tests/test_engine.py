# tests/test_engine.py
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from pathlib import Path

from tests.helpers import find_update_payload, http_status_error


def _make_case(**overrides):
    base = {
        "id": "case-uuid-1",
        "law_firm_id": "firm-uuid-1",
        "case_number": "C-1234-2024",
        "case_type": "rol",
        "matter": "civil",
        "status": "active",
        "assigned_user_id": "user-uuid-1",
        "consecutive_sync_failures": 3,
        "external_case_key": None,
    }
    base.update(overrides)
    return base


def _mock_search_response(found=True, blocked=False, matches=None):
    if matches is None and found:
        matches = [
            {
                "key": "eyJdetailkey",
                "rol": "C-1234-2024",
                "tribunal": "Juzgado Civil",
                "caratulado": "TEST vs TEST",
                "fecha_ingreso": "2024-01-15",
            }
        ]
    return {
        "found": found,
        "match_count": len(matches) if matches else 0,
        "matches": matches or [],
        "blocked": blocked,
        "error": None,
    }


def _mock_detail_response(blocked=False):
    return {
        "metadata": {
            "rol": "C-1234-2024",
            "tribunal": "Juzgado Civil",
            "estado_administrativo": "Sin archivar",
            "procedimiento": "Ordinario",
            "estado_procesal": "Tramitación",
            "etapa": "Discusión",
        },
        "movements": [
            {
                "folio": 1,
                "cuaderno": "Principal",
                "etapa": "Discusión",
                "tramite": "Resolución",
                "descripcion": "Provee demanda",
                "fecha": "2024-06-15",
                "foja": None,
                "documento_url": None,
            },
            {
                "folio": 2,
                "cuaderno": "Principal",
                "etapa": "Discusión",
                "tramite": "Escrito",
                "descripcion": "Contestación",
                "fecha": "2024-07-01",
                "foja": None,
                "documento_url": None,
            },
        ],
        "litigantes": [
            {"rol": "Demandante", "rut": "12345678-9", "nombre": "Juan Test"},
        ],
        "blocked": blocked,
        "error": None,
    }


def _make_engine(mock_sb=None, mock_pool=None, mock_notifier=None,
                 mock_metrics=None, mock_backoff=None):
    """Build a SyncEngine with all mocked dependencies."""
    from worker.engine import SyncEngine

    if mock_pool is None:
        mock_session = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_session)
        mock_pool.release = AsyncMock()
        mock_pool.enforce_global_rate_limit = AsyncMock()

    if mock_sb is None:
        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.insert.return_value = chain
        chain.select.return_value = chain
        chain.single.return_value = chain
        chain.execute.return_value = MagicMock(data={"id": "sync-run-1"}, count=0)
        chain.update.return_value = chain
        chain.eq.return_value = chain
        chain.order.return_value = chain
        chain.range.return_value = chain
        chain.upsert.return_value = chain
        chain.in_.return_value = chain

    if mock_notifier is None:
        mock_notifier = AsyncMock()
    if mock_metrics is None:
        mock_metrics = MagicMock()
    if mock_backoff is None:
        mock_backoff = MagicMock()

    engine = SyncEngine(
        pool=mock_pool,
        supabase=mock_sb,
        notifier=mock_notifier,
        metrics=mock_metrics,
        backoff=mock_backoff,
        config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
    )
    return engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff


class TestSyncEngine:
    @pytest.mark.asyncio
    async def test_sync_success_full_flow(self):
        from worker.engine import SyncEngine

        mock_session = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_session)
        mock_pool.release = AsyncMock()
        mock_pool.enforce_global_rate_limit = AsyncMock()

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.insert.return_value = chain
        chain.select.return_value = chain
        chain.single.return_value = chain
        chain.execute.return_value = MagicMock(data={"id": "sync-run-1"}, count=0)
        chain.update.return_value = chain
        chain.eq.return_value = chain
        chain.order.return_value = chain
        chain.range.return_value = chain
        chain.upsert.return_value = chain
        chain.in_.return_value = chain

        mock_notifier = AsyncMock()
        mock_metrics = MagicMock()
        mock_backoff = MagicMock()

        engine = SyncEngine(
            pool=mock_pool,
            supabase=mock_sb,
            notifier=mock_notifier,
            metrics=mock_metrics,
            backoff=mock_backoff,
            config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
        )

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        mock_pool.acquire.assert_called_once()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=True)
        mock_backoff.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_success_resets_el_contador(self):
        """Un sync exitoso debe resetear consecutive_sync_failures a 0, no incrementarlo.

        Regresión: el path de éxito lo incrementaba, así que las causas sanas
        acumulaban intentos hasta cruzar _MAX_CONSECUTIVE_FAILURES y quedar a un solo
        error de la suspensión permanente.
        """
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(consecutive_sync_failures=20)

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True

        success_update = find_update_payload(mock_sb, last_sync_status="success")

        assert success_update is not None, "Se esperaba un update con last_sync_status='success'"
        assert success_update["consecutive_sync_failures"] == 0, (
            f"Un sync exitoso debe resetear consecutive_sync_failures a 0, "
            f"pero quedó en {success_update['consecutive_sync_failures']}"
        )

    @pytest.mark.asyncio
    async def test_sync_blocked_triggers_backoff(self):
        from worker.engine import SyncEngine

        mock_session = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_session)
        mock_pool.release = AsyncMock()
        mock_pool.enforce_global_rate_limit = AsyncMock()

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.insert.return_value = chain
        chain.select.return_value = chain
        chain.single.return_value = chain
        chain.execute.return_value = MagicMock(data={"id": "sync-run-1"}, count=0)
        chain.update.return_value = chain
        chain.eq.return_value = chain

        mock_notifier = AsyncMock()
        mock_metrics = MagicMock()
        mock_backoff = MagicMock()

        engine = SyncEngine(
            pool=mock_pool,
            supabase=mock_sb,
            notifier=mock_notifier,
            metrics=mock_metrics,
            backoff=mock_backoff,
            config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
        )

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.return_value = _mock_search_response(blocked=True)
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_called_once()
        # Per-slot reactive re-mint on block: release(healthy=False).
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=False)
        # Anti-apagón: a block must never penalize the case via _update_case_error
        # nor increment consecutive_sync_failures.
        mock_update_error.assert_not_called()
        assert result.get("status") is None
        update_calls = chain.update.call_args_list
        for call in update_calls:
            payload = call[0][0] if call[0] else {}
            assert "consecutive_sync_failures" not in payload

    @pytest.mark.asyncio
    async def test_sync_invalid_identifier(self):
        from worker.engine import SyncEngine

        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=AsyncMock())
        mock_pool.release = AsyncMock()
        mock_pool.enforce_global_rate_limit = AsyncMock()

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.insert.return_value = chain
        chain.select.return_value = chain
        chain.single.return_value = chain
        chain.execute.return_value = MagicMock(data={"id": "sync-run-1"}, count=0)
        chain.update.return_value = chain
        chain.eq.return_value = chain

        engine = SyncEngine(
            pool=mock_pool,
            supabase=mock_sb,
            notifier=AsyncMock(),
            metrics=MagicMock(),
            backoff=MagicMock(),
            config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
        )

        case = _make_case(case_number="INVALID")
        result = await engine.sync_case(case)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_sync_unsupported_matter(self):
        """Cases with unsupported matter type should fail gracefully."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(matter="familia")
        result = await engine.sync_case(case)

        assert result["success"] is False
        assert result["new_movements"] == 0
        mock_metrics.record_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_not_found_in_ojv(self):
        """When search returns not found, sync should fail."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = _mock_search_response(found=False, matches=[])
            result = await engine.sync_case(case)

        assert result["success"] is False
        assert result["new_movements"] == 0
        mock_metrics.record_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_detail_blocked(self):
        """When detail fetch is blocked, backoff should be triggered."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()
        mock_session = mock_pool.acquire.return_value

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response(blocked=True)
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_called_once()
        mock_metrics.record_error.assert_called_once()
        # Per-slot reactive re-mint on block: release(healthy=False).
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=False)
        # Anti-apagón: a block must never penalize the case via _update_case_error.
        mock_update_error.assert_not_called()
        assert result.get("status") is None

    @pytest.mark.asyncio
    async def test_sync_parse_suspect_short_circuits_without_clobber(self):
        """parse_suspect NO debe seguir al path de éxito (que haría upsert vacío
        y sobrescribiría el external_payload marcando success). Debe cortar antes
        del upsert y delegar en _handle_parse_suspect."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case()
        detail = _mock_detail_response()
        detail["parse_suspect"] = True

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail, \
             patch.object(engine, "_upsert_movements", new_callable=AsyncMock) as mock_upsert, \
             patch.object(engine, "_handle_parse_suspect", new_callable=AsyncMock) as mock_hps:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = detail
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_hps.assert_called_once()
        mock_upsert.assert_not_called()  # no clobber del payload bueno
        mock_backoff.record_blocked.assert_not_called()  # no es un bloqueo
        mock_metrics.record_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_timeout_is_infra_non_penalizing(self):
        """A timeout remints only its session; it does not open the OJV breaker."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()
        mock_session = mock_pool.acquire.return_value

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.side_effect = TimeoutError("timed out")
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_not_called()
        mock_backoff.record_failure.assert_not_called()
        mock_metrics.record_error.assert_called_once()
        mock_update_error.assert_not_called()
        assert result["status"] == "pjud_timeout"
        assert find_update_payload(mock_sb, last_sync_status="pjud_timeout") is not None
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=False)
        update_calls = mock_sb.from_.return_value.update.call_args_list
        for call in update_calls:
            payload = call[0][0] if call[0] else {}
            assert "consecutive_sync_failures" not in payload

    @pytest.mark.asyncio
    async def test_sync_upstream_changed_keeps_session_healthy_without_breaker(self):
        """Valid PJUD HTML with parser drift is operationally distinct from WAF."""
        from app.failure_kind import UpstreamChangedError

        engine, mock_pool, mock_sb, _notifier, mock_metrics, mock_backoff = _make_engine()
        mock_session = mock_pool.acquire.return_value

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.side_effect = UpstreamChangedError("unexpected PJUD markup")
            result = await engine.sync_case(_make_case())

        assert result == {"success": False, "new_movements": 0, "status": "upstream_changed"}
        mock_backoff.record_blocked.assert_not_called()
        mock_backoff.record_failure.assert_not_called()
        mock_update_error.assert_not_called()
        mock_metrics.record_error.assert_called_once_with("infra")
        assert find_update_payload(mock_sb, last_sync_status="upstream_changed") is not None
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=True)

    @pytest.mark.asyncio
    async def test_sync_transport_error_is_infra_non_penalizing(self):
        """G2: httpx.ConnectError (dead residential proxy IP) must be treated
        as an infra failure, not a case-fault error. Same non-penalizing
        block-like handling as timeouts."""
        import httpx
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()
        mock_session = mock_pool.acquire.return_value

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.side_effect = httpx.ConnectError("boom")
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_called_once()
        mock_metrics.record_error.assert_called_once()
        mock_update_error.assert_not_called()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=False)

    @pytest.mark.asyncio
    async def test_sync_read_timeout_is_infra_non_penalizing(self):
        """G2: httpx.ReadTimeout is also an httpx.TransportError subclass and
        must be caught by the same infra handler (proves the base-class
        catch, not just ConnectError)."""
        import httpx
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()
        mock_session = mock_pool.acquire.return_value

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.side_effect = httpx.ReadTimeout("read timed out")
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_not_called()
        mock_update_error.assert_not_called()
        assert result["status"] == "pjud_timeout"
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=False)

    @pytest.mark.asyncio
    async def test_sync_proxy_error_is_infra_non_penalizing(self):
        """G2: httpx.ProxyError (proxy-level failure) must also be caught by
        the httpx.TransportError base-class handler."""
        import httpx
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()
        mock_session = mock_pool.acquire.return_value

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.side_effect = httpx.ProxyError("proxy boom")
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_called_once()
        mock_update_error.assert_not_called()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=False)

    @pytest.mark.asyncio
    async def test_sync_proxy_402_trips_persistent_billing_breaker_without_raw_user_error(self):
        import httpx
        engine, mock_pool, _sb, _notifier, mock_metrics, mock_backoff = _make_engine()
        mock_control = AsyncMock()
        engine._proxy_control = mock_control

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_finish_run", new_callable=AsyncMock) as mock_finish, \
             patch.object(engine, "_update_case_blocked", new_callable=AsyncMock) as mock_blocked, \
             patch("worker.engine.send_ops_alert", new_callable=AsyncMock):
            mock_search.side_effect = httpx.ProxyError("CONNECT failed: 402 Payment Required")
            result = await engine.sync_case(_make_case())

        assert result["status"] == "proxy_billing_exhausted"
        mock_control.trip_billing_exhausted.assert_awaited_once()
        mock_backoff.open_permanently.assert_called_once_with("billing_exhausted")
        assert mock_finish.await_args.args[4] == "infra_unavailable"
        assert "402" not in str(mock_blocked.await_args)
        mock_metrics.record_error.assert_called_with("infra")
        mock_pool.release.assert_awaited_once_with(
            mock_pool.acquire.return_value,
            healthy=False,
            remint=False,
        )

    @pytest.mark.asyncio
    async def test_budget_denial_pauses_worker_without_penalizing_or_contacting_pjud(self):
        from app.proxy_cost import ProxyBudgetExceededError

        engine, mock_pool, _sb, _notifier, mock_metrics, mock_backoff = _make_engine()
        mock_control = AsyncMock()
        engine._proxy_control = mock_control

        class DeniedTracker:
            @asynccontextmanager
            async def track(self, **_kwargs):
                raise ProxyBudgetExceededError("case")
                yield

        engine._proxy_usage = DeniedTracker()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as search, \
             patch.object(engine, "_finish_run", new_callable=AsyncMock) as finish, \
             patch.object(engine, "_update_case_blocked", new_callable=AsyncMock) as blocked, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as case_error, \
             patch("worker.engine.send_ops_alert", new_callable=AsyncMock) as alert:
            result = await engine.sync_case(_make_case())

        assert result["status"] == "proxy_budget_blocked"
        search.assert_not_awaited()
        case_error.assert_not_awaited()
        mock_control.refresh.assert_not_awaited()
        mock_backoff.open_permanently.assert_not_called()
        assert finish.await_args.args[4] == "infra_unavailable"
        assert "budget detail" not in str(blocked.await_args)
        mock_metrics.record_error.assert_called_with("infra")
        mock_pool.release.assert_awaited_once_with(
            mock_pool.acquire.return_value, healthy=True,
        )
        assert alert.await_args.args[2] == "proxy_budget_blocked"
        assert "case" in alert.await_args.args[3]

    @pytest.mark.asyncio
    async def test_global_budget_denial_opens_global_breaker(self):
        from app.proxy_cost import ProxyBudgetExceededError

        engine, mock_pool, _sb, _notifier, _metrics, mock_backoff = _make_engine()
        engine._proxy_control = AsyncMock()

        class DeniedTracker:
            @asynccontextmanager
            async def track(self, **_kwargs):
                raise ProxyBudgetExceededError("global")
                yield

        engine._proxy_usage = DeniedTracker()
        with patch.object(engine, "_finish_run", new_callable=AsyncMock), \
             patch.object(engine, "_update_case_blocked", new_callable=AsyncMock), \
             patch("worker.engine.send_ops_alert", new_callable=AsyncMock):
            result = await engine.sync_case(_make_case())

        assert result["status"] == "proxy_cost_control_paused"
        engine._proxy_control.refresh.assert_awaited_once()
        mock_backoff.open_permanently.assert_called_once_with("proxy_cost_control")
        mock_pool.release.assert_awaited_once_with(
            mock_pool.acquire.return_value, healthy=True,
        )

    @pytest.mark.asyncio
    async def test_sync_5xx_de_ojv_no_penaliza_y_se_le_atribuye_a_ojv(self):
        """Un 503 de OJV NO puede suspender la causa.

        `httpx.HTTPStatusError` no es `httpx.TransportError` —son hermanos bajo
        `HTTPError`—, así que la lista vieja lo dejaba caer al `except Exception`
        genérico: `_update_case_error`, contador++, y a las 10 la causa
        `suspended` por una tarde en que el portal de ellos estuvo caído. La app
        ya lo clasificaba bien (`pjudHttpError`, `status >= 500`) y los dos
        servicios escriben sobre las mismas filas, así que el desenlace dependía
        de cuál de los dos tomara la causa.
        """
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()
        mock_session = mock_pool.acquire.return_value

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error, \
             patch.object(engine, "_handle_blocked", new_callable=AsyncMock) as mock_blocked:
            mock_search.side_effect = http_status_error(503)
            result = await engine.sync_case(_make_case())

        assert result["success"] is False
        mock_update_error.assert_not_called()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=False)
        # Y con la culpa donde corresponde: el 503 salió del servidor de ellos.
        assert mock_blocked.await_args[0][1] == "ojv"

    @pytest.mark.asyncio
    async def test_sync_404_de_ojv_si_es_de_la_causa(self):
        """El otro lado de la simetría: un 404 SÍ describe algo del pedido.

        Sin este test, "no penalizar por HTTPStatusError" pasaría también con la
        regla entera invertida —nada penalizaría nunca— y el techo de suspensión
        quedaría muerto sin que nada lo dijera.
        """
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.side_effect = http_status_error(404)
            result = await engine.sync_case(_make_case())

        assert result["success"] is False
        mock_update_error.assert_called_once()
        mock_backoff.record_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_cuerpo_vacio_es_infra_y_no_bloqueo_de_ojv(self):
        """Cero bytes es el túnel cortándose, no el WAF de OJV.

        Una página contentless de ~39 bytes SÍ es soft-block de F5 y sigue
        contando como bloqueo (ver `test_returns_blocked_on_contentless_soft_block`);
        cero bytes es la respuesta que no llegó, y cargársela a OJV le escribe al
        abogado "bloqueado por OJV" cuando lo que hay que revisar es el proxy.
        """
        from app.failure_kind import EmptyResponseError
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error, \
             patch.object(engine, "_handle_blocked", new_callable=AsyncMock) as mock_blocked:
            mock_search.side_effect = EmptyResponseError("search: OJV devolvio un cuerpo vacio")
            result = await engine.sync_case(_make_case())

        assert result["success"] is False
        mock_update_error.assert_not_called()
        assert mock_blocked.await_args[0][1] == "infra"

    @pytest.mark.asyncio
    async def test_sync_generic_exception_still_penalizes(self):
        """Regression: a genuine unexpected bug (e.g. ValueError deep in
        parsing) is NOT an infra failure and must still go through the
        generic Exception path — _update_case_error IS called, record_failure
        (not record_blocked), and the session is released healthy=True."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()
        mock_session = mock_pool.acquire.return_value

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.side_effect = ValueError("deep parsing bug")
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_update_error.assert_called_once()
        mock_backoff.record_failure.assert_called_once()
        mock_backoff.record_blocked.assert_not_called()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=True)

    @pytest.mark.asyncio
    async def test_sync_prefers_fresh_search_key_over_stored(self):
        """Fresh search key should be preferred over stored external_case_key (JWT may expire)."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(external_case_key="eyJpreexisting_key")

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()  # returns "eyJdetailkey"
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        # Should use fresh key from search, not the stored (potentially expired) key
        call_args = mock_detail.call_args
        assert call_args[0][2] == "eyJdetailkey"

    @pytest.mark.asyncio
    async def test_sync_notifier_called_when_new_movements(self):
        """Notifier should be called when there are new movements."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        # Simulate 2 new movements by having before=0, after=2
        call_count = 0

        def side_effect_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # For count queries: first call returns count=0, second returns count=2
            # We track calls to distinguish the before/after count queries
            return MagicMock(data={"id": "sync-run-1"}, count=call_count * 2)

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.insert.return_value = chain
        chain.select.return_value = chain
        chain.single.return_value = chain
        chain.update.return_value = chain
        chain.eq.return_value = chain
        chain.order.return_value = chain
        chain.range.return_value = chain
        chain.upsert.return_value = chain
        chain.in_.return_value = chain

        # Return 0 for before-count, 2 for after-count
        execute_returns = [
            MagicMock(data={"id": "sync-run-1"}, count=None),  # sync run insert
            MagicMock(data=[], count=None),  # existing movement identities
            MagicMock(data=[], count=0),   # before count
            MagicMock(data=[], count=None),  # upsert
            MagicMock(data=[], count=2),   # after count
            MagicMock(data=[], count=None),  # cases update
            MagicMock(data=[], count=None),  # finish sync run
        ]
        execute_call_count = [0]

        def controlled_execute():
            idx = execute_call_count[0]
            execute_call_count[0] += 1
            if idx < len(execute_returns):
                return execute_returns[idx]
            return MagicMock(data=[], count=None)

        chain.execute.side_effect = controlled_execute

        from worker.engine import SyncEngine
        engine = SyncEngine(
            pool=engine._pool,
            supabase=mock_sb,
            notifier=mock_notifier,
            metrics=mock_metrics,
            backoff=mock_backoff,
            config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
        )

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        assert result["new_movements"] == 2
        mock_notifier.notify_new_movements.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_session_released_on_error(self):
        """Session must be released even when an exception occurs."""
        mock_session = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_session)
        mock_pool.release = AsyncMock()
        mock_pool.enforce_global_rate_limit = AsyncMock()

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.insert.return_value = chain
        chain.select.return_value = chain
        chain.single.return_value = chain
        chain.execute.return_value = MagicMock(data={"id": "sync-run-1"}, count=0)
        chain.update.return_value = chain
        chain.eq.return_value = chain

        from worker.engine import SyncEngine
        engine = SyncEngine(
            pool=mock_pool,
            supabase=mock_sb,
            notifier=AsyncMock(),
            metrics=MagicMock(),
            backoff=MagicMock(),
            config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
        )

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = RuntimeError("unexpected crash")
            result = await engine.sync_case(case)

        assert result["success"] is False
        # Session must be released in finally block. A generic (non-block)
        # exception is not an "IP is bad" signal, so healthy stays True.
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=True)

    @pytest.mark.asyncio
    async def test_sync_apelaciones_uses_corte_from_external_payload(self):
        """For apelaciones cases, corte should be read from external_payload."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(
            case_number="Proteccion-4490-2025",
            matter="apelaciones",
            external_case_key=None,
            external_payload={"corte": 91},
        )

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        # Verify that search was called with corte=91 in form_data
        call_args = mock_search.call_args
        form_data = call_args[0][2]  # third positional arg is form_data
        assert form_data["conCorte"] == "91"

    @pytest.mark.asyncio
    async def test_sync_apelaciones_warns_when_no_corte(self):
        """For apelaciones cases without corte, a warning should be logged."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(
            case_number="Proteccion-4490-2025",
            matter="apelaciones",
            external_case_key=None,
            external_payload={},
        )

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail, \
             patch("worker.engine.logger") as mock_logger:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        mock_logger.warning.assert_any_call(
            "No court_code for apelaciones case %s; searching all cortes",
            "Proteccion-4490-2025",
        )

    @pytest.mark.asyncio
    async def test_sync_reuses_all_persisted_canonical_search_inputs(self):
        """A canonical RIT must keep its court, tribunal, libro and radio.

        This catches the worker silently falling back to the legacy parser/form,
        which loses the persisted PJUD identity after the case was confirmed.
        """
        engine, _pool, _sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(
            matter="penal",
            case_number="O-243-2025",
            case_type="rit",
            court_code=90,
            tribunal_code=1222,
            libro="1",
            pjud_search_mode=None,
            tribunal_unknown=False,
        )

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response(matches=[{
                "key": "eyJpenalkey",
                "rol": "O-243-2025",
                "tribunal": "3° Juzgado de Garantía de Santiago",
                "caratulado": "TEST",
                "fecha_ingreso": "2025-01-15",
            }])
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        form = mock_search.call_args.args[2]
        assert form["conCorte"] == "90"
        assert form["conTribunal"] == "1222"
        assert form["conTipoCausa"] == "1"
        assert form["radio-groupPenal"] == "1"
        assert mock_search.call_args.args[0] is mock_detail.call_args.args[0]

    @pytest.mark.asyncio
    async def test_sync_enriches_real_parser_candidate_with_official_catalog(self):
        """The worker must resolve a parser label through one official catalog lookup."""
        engine, mock_pool, _sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(
            matter="penal",
            case_number="O-100-2025",
            case_type="rit",
            court_code=90,
            tribunal_code=1223,
            libro="1",
            tribunal_unknown=False,
        )
        fixture = (Path(__file__).parent / "fixtures" / "search_Penal_O_100_2025.html").read_text()
        mock_pool.acquire.return_value.search = AsyncMock(return_value=fixture)

        with patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        mock_detail.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_broad_ambiguous_candidates_require_resolution_without_detail(self):
        """A broad canonical search can never send matches[0] to detail.

        Removing exact-match correlation (or restoring the old matches[0]
        selection) makes this test fail by fetching an unrelated expediente.
        """
        engine, _pool, mock_sb, _notifier, mock_metrics, mock_backoff = _make_engine()
        case = _make_case(
            court_code=None,
            tribunal_code=None,
            libro="C",
            pjud_search_mode=None,
            tribunal_unknown=True,
        )
        candidates = [
            {
                "key": "eyJfirst",
                "rol": "C-1234-2024",
                "tribunal": "1° Juzgado Civil de Santiago",
                "caratulado": "DUPLICATE",
                "fecha_ingreso": "2025-01-15",
            },
            {
                "key": "eyJsecond",
                "rol": "C-1234-2024",
                "tribunal": "2° Juzgado Civil de Santiago",
                "caratulado": "TARGET",
                "fecha_ingreso": "2024-01-15",
            },
        ]

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response(matches=candidates)
            result = await engine.sync_case(case)

        assert result == {"success": False, "new_movements": 0, "status": "needs_disambiguation"}
        mock_detail.assert_not_awaited()
        mock_backoff.record_failure.assert_not_called()
        mock_backoff.record_blocked.assert_not_called()
        mock_metrics.record_error.assert_called_once_with("resolution")
        resolution_update = find_update_payload(mock_sb, last_sync_status="needs_disambiguation")
        assert resolution_update is not None
        assert "consecutive_sync_failures" not in resolution_update

    @pytest.mark.asyncio
    async def test_invalid_canonical_identity_never_falls_back_to_first_legacy_match(self):
        """A populated but invalid v2 identity must not re-enable matches[0]."""
        engine, _pool, mock_sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(
            court_code=90,
            tribunal_code=321,
            libro="INVALID",
            tribunal_unknown=False,
        )
        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            result = await engine.sync_case(case)

        assert result["status"] == "invalid_identity"
        mock_search.assert_not_awaited()
        mock_detail.assert_not_awaited()
        assert find_update_payload(mock_sb, last_sync_status="invalid_identity") is not None

    @pytest.mark.asyncio
    async def test_duplicate_logical_candidates_do_not_create_false_ambiguity(self):
        """The same official row with a new JWT is not a second cause."""
        engine, _pool, _sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(court_code=90, tribunal_code=260, libro="C", tribunal_unknown=False)
        candidate = {
            "key": "eyJsame",
            "rol": "C-1234-2024",
            "tribunal": "2° Juzgado Civil de Santiago",
            "caratulado": "TARGET",
            "fecha_ingreso": "2024-01-15",
        }
        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response(matches=[
                candidate,
                {**candidate, "key": "eyJfreshJWT"},
            ])
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        mock_detail.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_known_identity_rejects_unresolved_official_code_without_detail(self):
        """A parser label not present in the catalog is not neutral evidence."""
        engine, _pool, _sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(court_code=90, tribunal_code=260, libro="C", tribunal_unknown=False)

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response(matches=[{
                "key": "eyJunresolved",
                "rol": "C-1234-2024",
                "tribunal": "Tribunal inexistente en catalogo",
                "caratulado": "TARGET",
                "fecha_ingreso": "2024-01-15",
            }])
            result = await engine.sync_case(case)

        assert result["status"] == "needs_disambiguation"
        mock_detail.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_rejects_exact_identifier_with_conflicting_official_tribunal(self):
        """Known canonical tribunal codes are a hard anti-contamination signal."""
        engine, _pool, _sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(
            court_code=90,
            tribunal_code=321,
            libro="C",
            pjud_search_mode=None,
            tribunal_unknown=False,
        )

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response(matches=[{
                "key": "eyJwrongtribunal",
                "rol": "C-1234-2024",
                "tribunal": "9° Juzgado Civil de Santiago",
                "caratulado": "TARGET",
                "fecha_ingreso": "2024-01-15",
            }])
            result = await engine.sync_case(case)

        assert result["status"] == "needs_disambiguation"
        mock_detail.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_learns_broad_tribunal_only_after_confirmed_detail(self):
        """A real parser row learns both official codes only after detail."""
        engine, mock_pool, mock_sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(
            matter="penal",
            case_number="O-100-2025",
            case_type="rit",
            court_code=None,
            tribunal_code=None,
            libro="1",
            pjud_search_mode=None,
            tribunal_unknown=True,
        )
        fixture = (Path(__file__).parent / "fixtures" / "search_Penal_O_100_2025.html").read_text()
        mock_pool.acquire.return_value.search = AsyncMock(return_value=fixture)

        with patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        learned_update = find_update_payload(mock_sb, tribunal_code=1223)
        assert learned_update is not None
        assert learned_update["court_code"] == 90
        assert learned_update["court"] == "4º Juzgado de Garantía de Santiago"
        assert learned_update["tribunal_unknown"] is False
        chain = mock_sb.from_.return_value
        chain.eq.assert_any_call("tribunal_unknown", True)
        chain.is_.assert_any_call("tribunal_code", "null")
        chain.is_.return_value.is_.assert_called_once_with("court_code", "null")

    @pytest.mark.asyncio
    async def test_lost_identity_cas_aborts_before_movements_payload_or_success(self):
        """A concurrent human choice wins before any irreversible sync effect."""
        class UpdateFilterBuilderWithoutSelect:
            """postgrest's update filter builder deliberately has no select()."""

            def __init__(self):
                self.filters = []

            def eq(self, field, value):
                self.filters.append(("eq", field, value))
                return self

            def is_(self, field, value):
                self.filters.append(("is", field, value))
                return self

            def execute(self):
                return MagicMock(data=[])

        engine, _pool, mock_sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(court_code=None, tribunal_code=None, libro="C", tribunal_unknown=True)
        chain = mock_sb.from_.return_value
        cas_builder = UpdateFilterBuilderWithoutSelect()
        chain.is_.return_value = cas_builder

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail, \
             patch.object(engine, "_upsert_movements", new_callable=AsyncMock) as mock_upsert:
            mock_search.return_value = _mock_search_response(matches=[{
                "key": "eyJtarget",
                "rol": "C-1234-2024",
                "tribunal": "2° Juzgado Civil de Santiago",
                "caratulado": "TARGET",
                "fecha_ingreso": "2024-01-15",
            }])
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result == {"success": False, "new_movements": 0, "status": "identity_changed"}
        mock_detail.assert_awaited_once()
        mock_upsert.assert_not_awaited()
        assert find_update_payload(mock_sb, last_sync_status="success") is None
        payloads = [call.args[0] for call in chain.update.call_args_list]
        assert not any("external_payload" in payload for payload in payloads)
        chain.eq.assert_any_call("id", case["id"])
        chain.eq.assert_any_call("tribunal_unknown", True)
        chain.is_.assert_called_once_with("tribunal_code", "null")
        assert cas_builder.filters == [
            ("is", "court_code", "null"),
        ]

    @pytest.mark.asyncio
    async def test_real_one_slot_pool_finishes_broad_sync_without_nested_acquire(self):
        """A real SessionPool(1) must not deadlock resolving a broad result."""
        from worker.session_pool import SessionPool, _Slot

        config = MagicMock(
            OJV_PROXY_URL="http://proxy.example",
            OJV_PROXY_POOL_SIZE=1,
            POOL_SIZE=1,
            SESSION_MAX_AGE_S=1500,
            OJV_PROXY_STICKY_LIFETIME="1h",
            COOKIE_STORE_PATH="/tmp/pjud-worker-test-cookies.json",
            PJUD_BASE_URL="https://pjud.example",
            RATE_LIMIT_MS=0,
            BLOCK_PAUSE_S=30,
            MINT_MAX_RETRIES=1,
            OJV_TIMEOUT_S=25,
            R2_ENABLED=False,
        )
        pool = SessionPool(config)
        session = MagicMock(age_seconds=0)
        fixture = (Path(__file__).parent / "fixtures" / "search_Penal_O_100_2025.html").read_text()
        session.search = AsyncMock(return_value=fixture)
        pool._slots = [_Slot(index=0, session=session)]
        engine, _unused_pool, mock_sb, _notifier, _metrics, _backoff = _make_engine(mock_pool=pool)
        case = _make_case(
            matter="penal", case_number="O-100-2025", case_type="rit",
            court_code=None, tribunal_code=None, libro="1", tribunal_unknown=True,
        )

        with patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_detail.return_value = _mock_detail_response()
            result = await asyncio.wait_for(engine.sync_case(case), timeout=0.5)

        assert result["success"] is True
        assert pool._sem._value == 1
        assert find_update_payload(mock_sb, tribunal_code=1223)["court_code"] == 90

    @pytest.mark.asyncio
    async def test_appeals_first_instance_broad_keeps_known_court_and_learns_tribunal(self):
        """Appeals broad resolution is scoped to the persisted Corte de Apelaciones."""
        engine, _pool, mock_sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(
            matter="apelaciones", case_number="4490-2025", case_type="rol",
            court_code=90, tribunal_code=None, libro=None,
            pjud_search_mode="first_instance", tribunal_unknown=True,
        )

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response(matches=[{
                "key": "eyJappeal-first-instance",
                "rol": "4490-2025",
                "tribunal": "2° Juzgado Civil de Santiago",
                "caratulado": "TARGET",
                "fecha_ingreso": "2025-01-15",
            }])
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        learned = find_update_payload(mock_sb, tribunal_code=260)
        assert learned is not None
        assert "court_code" not in learned

    @pytest.mark.asyncio
    async def test_sync_legacy_default_false_without_complete_identity_keeps_v1(self):
        """A staged row may expose the new boolean default before its codes.

        Treating that row as v2 would reject an existing cause instead of using
        the legacy query during the microservice-first deployment.
        """
        engine, _pool, _sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(tribunal_unknown=False)

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        form = mock_search.call_args.args[2]
        assert form["conCorte"] == "0"
        assert form["conTribunal"] == "0"

    @pytest.mark.asyncio
    async def test_sync_canonical_appeals_resource_keeps_book_without_tribunal(self):
        """A direct appeal uses its official book and never invents a tribunal."""
        engine, _pool, _sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(
            matter="apelaciones",
            case_number="4490-2025",
            case_type="rol",
            court_code=90,
            tribunal_code=None,
            libro="34",
            pjud_search_mode="appeals_resource",
            tribunal_unknown=False,
        )

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response(matches=[{
                "key": "eyJappealkey",
                "rol": "Protección-4490-2025",
                "tribunal": "Corte de Apelaciones de Santiago",
                "caratulado": "TEST",
                "fecha_ingreso": "2025-01-15",
                "libro_code": "34",
            }])
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        form = mock_search.call_args.args[2]
        assert form["conCorte"] == "90"
        assert form["conTribunal"] == "0"
        assert form["conTipoCausa"] == "PROTECCION"
        assert form["conTipoBusApe"] == "0"

    @pytest.mark.asyncio
    async def test_sync_canonical_ruc_bypasses_legacy_parser_and_finishes_successfully(self):
        """RUC is not an X-NNN-YYYY identifier, but it is valid PJUD v2 input."""
        engine, _pool, mock_sb, _notifier, _metrics, _backoff = _make_engine()
        case = _make_case(
            matter="penal",
            case_number="2400100001-5",
            case_type="ruc",
            court_code=90,
            tribunal_code=1222,
            libro=None,
            pjud_search_mode=None,
            tribunal_unknown=False,
        )

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response(matches=[
                {
                    "key": "eyJwrongruckey",
                    "rol": "O-243-2025",
                    "ruc": "2400100002-5",
                    "tribunal": "3° Juzgado de Garantía de Santiago",
                    "caratulado": "WRONG RUC",
                    "fecha_ingreso": "2025-01-15",
                    "tribunal_code": 1222,
                },
                {
                    "key": "eyJruckey",
                    "rol": "O-999-2025",
                    "ruc": "2400100001-5",
                    "tribunal": "3° Juzgado de Garantía de Santiago",
                    "caratulado": "TEST",
                    "fecha_ingreso": "2025-01-15",
                    "tribunal_code": 1222,
                },
            ])
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        form = mock_search.call_args.args[2]
        assert form["radio-groupPenal"] == "2"
        assert form["rucPen1"] == "2400100001"
        assert form["rucPen2"] == "5"
        assert mock_detail.call_args.args[2] == "eyJruckey"
        success_update = find_update_payload(mock_sb, last_sync_status="success")
        assert success_update["canonical_identifier"] == "penal:ruc:2400100001-5"

    @pytest.mark.asyncio
    async def test_sync_records_sync_on_success(self):
        """metrics.record_sync() should be called on success."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        mock_metrics.record_sync.assert_called_once()
        mock_metrics.record_error.assert_not_called()


class TestHelperFunctions:
    def test_compute_priority_closed_case(self):
        from worker.engine import _compute_priority
        assert _compute_priority("closed", "2024-01-01") == 4

    def test_compute_priority_archived_case(self):
        from worker.engine import _compute_priority
        assert _compute_priority("archived", "2024-01-01") == 4

    def test_compute_priority_recent_movement_is_daily_without_explicit_urgency(self):
        from worker.engine import _compute_priority
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=3)).isoformat()
        assert _compute_priority("active", recent, is_urgent=False) == 2

    def test_compute_priority_explicit_urgency(self):
        from worker.engine import _compute_priority
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=3)).isoformat()
        assert _compute_priority("active", recent, is_urgent=True) == 1

    def test_compute_priority_medium_age_movement(self):
        from worker.engine import _compute_priority
        from datetime import date, timedelta
        medium = (date.today() - timedelta(days=20)).isoformat()
        assert _compute_priority("active", medium) == 2

    def test_compute_priority_old_movement(self):
        from worker.engine import _compute_priority
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=45)).isoformat()
        assert _compute_priority("active", old) == 3

    def test_compute_priority_no_latest_date(self):
        from worker.engine import _compute_priority
        assert _compute_priority("active", None) == 2

    def test_compute_priority_invalid_date(self):
        from worker.engine import _compute_priority
        assert _compute_priority("active", "not-a-date") == 2

    def test_compute_next_sync_at_returns_iso(self):
        from worker.engine import _compute_next_sync_at
        result = _compute_next_sync_at(1)
        # Should be a valid ISO datetime string
        datetime.fromisoformat(result)

    def test_adaptive_intervals_reduce_polling_for_inactive_cases(self):
        from worker.engine import SYNC_INTERVALS_HOURS

        assert SYNC_INTERVALS_HOURS == {
            1: 6,
            2: 24,
            3: 168,
            4: 168,
        }

    def test_compute_next_sync_at_priority_4_weekly(self):
        from worker.engine import _compute_next_sync_at, SYNC_INTERVALS_HOURS
        from datetime import datetime
        from zoneinfo import ZoneInfo
        before = datetime.now(ZoneInfo("America/Santiago"))
        result = _compute_next_sync_at(4)
        after = datetime.fromisoformat(result)
        diff_hours = (after - before).total_seconds() / 3600
        assert 167 < diff_hours < 169  # ~168 hours

    def test_get_latest_movement_date_returns_most_recent(self):
        from worker.engine import _get_latest_movement_date
        movements = [
            {"fecha": "2024-01-15"},
            {"fecha": "2024-07-01"},
            {"fecha": "2024-03-20"},
        ]
        assert _get_latest_movement_date(movements) == "2024-07-01"

    def test_get_latest_movement_date_empty(self):
        from worker.engine import _get_latest_movement_date
        assert _get_latest_movement_date([]) is None

    def test_get_latest_movement_date_missing_fecha(self):
        from worker.engine import _get_latest_movement_date
        movements = [{"cuaderno": "Principal"}, {"fecha": None}]
        assert _get_latest_movement_date(movements) is None

    def test_build_external_movement_key(self):
        from worker.engine import _build_external_movement_key
        key = _build_external_movement_key("C-1234-2024", "Principal", 5)
        assert key == "C-1234-2024:Principal:5"

    def test_build_movement_key_preserves_historical_key_with_folio(self):
        from worker.engine import _build_movement_external_key

        key = _build_movement_external_key(
            "C-1234-2024",
            {"folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01"},
        )

        assert key == "C-1234-2024:Principal:5"

    def test_null_folio_key_matches_canonical_cross_repo_vector(self):
        from worker.engine import _build_movement_external_key

        first = {
            "folio": None,
            "cuaderno": "Ｐrincipal",
            "fecha": " 2024-05-01 ",
            "tramite": "Resolucio\u0301n",
            "descripcion": " Provee\u00a0  demanda ",
            "etapa": "Discusión",
            "foja": None,
            "sala": "Primera",
            "estado": "Pendiente",
            "documento_url": "/documento",
            "documento_token": "jwt-1",
            "anexo_func": "anexoSolicitudCivil",
            "anexo_token": "jwt-2",
        }
        second = {**first, "descripcion": "Provee contestación"}

        first_key = _build_movement_external_key("C-1234-2024", first)

        assert first_key == (
            "pjud:null-folio:"
            "233b28b1647858c30529573ec89896d993ce6e22723d3fe73d053c3f7f65a84a"
        )
        assert first_key == _build_movement_external_key("C-1234-2024", dict(first))
        assert first_key != _build_movement_external_key("C-1234-2024", second)
        assert len(first_key) == 80

    def test_null_folio_key_uses_explicit_cross_runtime_whitespace_set(self):
        from worker.engine import _build_movement_external_key

        canonical = {
            "folio": None,
            "cuaderno": "Principal",
            "fecha": "2024-05-01",
            "tramite": "Resolución",
            "descripcion": "Provee demanda",
        }
        ecma_whitespace = {
            **canonical,
            "descripcion": (
                "\ufeffProvee\u1680\u2003\u2028\u2029\u202f\u205f\u3000demanda\ufeff"
            ),
        }
        python_only_whitespace = {
            **canonical,
            "descripcion": "Provee\u0085demanda",
        }

        assert _build_movement_external_key(
            "C-1234-2024", ecma_whitespace,
        ) == (
            "pjud:null-folio:"
            "233b28b1647858c30529573ec89896d993ce6e22723d3fe73d053c3f7f65a84a"
        )
        assert _build_movement_external_key(
            "C-1234-2024", python_only_whitespace,
        ) == (
            "pjud:null-folio:"
            "c782b1b1bff4d2943d4fc89584cf613b659689f07be20253606ee8ff3bc84942"
        )

    def test_null_folio_key_ignores_all_mutable_and_document_fields(self):
        from worker.engine import _build_movement_external_key

        movement = {
            "folio": None,
            "cuaderno": "Principal",
            "fecha": "2024-05-01",
            "tramite": "Resolución",
            "descripcion": "Provee demanda",
            "etapa": "Discusión",
            "foja": 1,
            "sala": "Primera",
            "estado": "Pendiente",
            "documento_url": "/doc-a",
            "documento_token": "token-a",
            "documentos_adicionales": [{"url": "/cert-a", "token": "cert-a"}],
            "anexo_func": "anexoSolicitudCivil",
            "anexo_token": "anexo-a",
        }
        changed = {
            **movement,
            "etapa": "Cumplimiento",
            "foja": 99,
            "sala": "Segunda",
            "estado": "Firmado",
            "documento_url": "/doc-b",
            "documento_token": "token-b",
            "documentos_adicionales": [{"url": "/cert-b", "token": "cert-b"}],
            "anexo_func": "anexoCausaCivil",
            "anexo_token": "anexo-b",
        }

        assert _build_movement_external_key(
            "C-1234-2024", movement,
        ) == _build_movement_external_key("C-1234-2024", changed)

    @pytest.mark.asyncio
    async def test_two_syncs_with_mutable_status_keep_key_and_do_not_renotify(self):
        from worker.engine import _prepare_pjud_movements

        engine, _, _, notifier, *_ = _make_engine()
        case = _make_case()
        base_movement = {
            "folio": None,
            "cuaderno": "Principal",
            "fecha": "2024-05-01",
            "tramite": "Resolución",
            "descripcion": "Provee demanda",
            "etapa": "Discusión",
            "foja": None,
            "sala": "Primera",
            "estado": "Pendiente",
        }
        first_detail = _mock_detail_response()
        first_detail["movements"] = [base_movement]
        second_detail = _mock_detail_response()
        second_detail["movements"] = [{
            **base_movement,
            "sala": "Segunda",
            "estado": "Firmado",
        }]
        seen_keys: set[str] = set()
        keys_by_sync: list[list[str]] = []

        async def fake_db_upsert(current_case, detail):
            keys = [
                key
                for _, key in _prepare_pjud_movements(
                    current_case, detail["movements"], log_undated=True,
                )
            ]
            keys_by_sync.append(keys)
            new_keys = [key for key in keys if key not in seen_keys]
            seen_keys.update(keys)
            return len(new_keys)

        with patch("worker.engine.search_pjud_via_session", new=AsyncMock(
                 return_value=_mock_search_response()
             )), \
             patch("worker.engine.detail_pjud_via_session", new=AsyncMock(
                 side_effect=[first_detail, second_detail]
             )), \
             patch.object(engine, "_upsert_movements", side_effect=fake_db_upsert):
            first_result = await engine.sync_case(case)
            second_result = await engine.sync_case(case)

        assert keys_by_sync[0] == keys_by_sync[1]
        assert first_result["new_movements"] == 1
        assert second_result["new_movements"] == 0
        notifier.notify_new_movements.assert_awaited_once_with(case, 1)

    @pytest.mark.asyncio
    async def test_upsert_movements_undated_only_returns_zero_without_db_calls(self):
        engine, _, mock_sb, *_ = _make_engine()
        detail = {"movements": [{
            "folio": None,
            "cuaderno": "Principal",
            "tramite": "Resolución",
            "descripcion": "Sin fecha",
            "fecha": None,
            "raw_payload": {"secret": "must-not-be-logged"},
        }]}

        result = await engine._upsert_movements(_make_case(), detail)

        assert result == 0
        mock_sb.from_.assert_not_called()

    @pytest.mark.asyncio
    async def test_upsert_movements_skips_undated_and_persists_dated_rows(self):
        engine, _, mock_sb, *_ = _make_engine()
        detail = {"movements": [
            {"folio": 4, "cuaderno": "Principal", "tramite": "Resolución",
             "descripcion": "Sin fecha", "fecha": None, "etapa": ""},
            {"folio": 5, "cuaderno": "Principal", "tramite": "Escrito",
             "descripcion": "Con fecha", "fecha": "2024-05-01", "etapa": ""},
        ]}

        await engine._upsert_movements(_make_case(), detail)

        rows = mock_sb.from_.return_value.upsert.call_args[0][0]
        assert len(rows) == 1
        assert rows[0]["date"] == "2024-05-01"
        assert rows[0]["external_movement_key"] == "C-1234-2024:Principal:5"

    @pytest.mark.asyncio
    async def test_upsert_movements_logs_one_structured_payload_free_warning(self, caplog):
        engine, _, _, *_ = _make_engine()
        detail = {"movements": [
            {"folio": None, "cuaderno": "Principal", "fecha": None,
             "tramite": "JWT super-secret", "descripcion": "litigante Jane Doe",
             "documento_token": "document-token", "raw_payload": {"rut": "1-9"}},
            {"folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
             "tramite": "Escrito", "descripcion": "Válido", "etapa": ""},
        ]}

        with caplog.at_level("WARNING", logger="worker.engine"):
            await engine._upsert_movements(_make_case(), detail)

        warnings = [record for record in caplog.records if record.msg == "Skipping undated PJUD movements"]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning.case_id == "case-uuid-1"
        assert warning.case_number == "C-1234-2024"
        assert warning.skipped_count == 1
        rendered = caplog.text
        assert "super-secret" not in rendered
        assert "document-token" not in rendered
        assert "Jane Doe" not in rendered
        assert "1-9" not in rendered

    @pytest.mark.asyncio
    async def test_identical_null_folio_movements_are_collapsed_before_upsert(self):
        engine, _, mock_sb, *_ = _make_engine()
        movement = {
            "folio": None, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Provee", "etapa": "Discusión",
            "foja": None, "sala": "", "estado": "",
        }

        await engine._upsert_movements(
            _make_case(), {"movements": [movement, dict(movement)]}
        )

        rows = mock_sb.from_.return_value.upsert.call_args[0][0]
        assert len(rows) == 1
        assert "#" not in rows[0]["external_movement_key"]

    @pytest.mark.asyncio
    async def test_logical_duplicate_does_not_consume_folio_suffix(self):
        engine, _, mock_sb, *_ = _make_engine()
        first = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Primera", "etapa": "",
        }
        different = {**first, "tramite": "", "descripcion": "Segunda"}

        await engine._upsert_movements(
            _make_case(),
            {"movements": [first, dict(first), different]},
        )

        rows = mock_sb.from_.return_value.upsert.call_args.args[0]
        assert [row["external_movement_key"] for row in rows] == [
            "C-1234-2024:Principal:5",
            "C-1234-2024:Principal:5#2",
        ]

    def test_same_identity_keeps_key_when_pjud_reverses_response_order(self):
        from worker.engine import _prepare_pjud_movements

        primary = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Movimiento principal",
            "etapa": "Discusión", "foja": 10,
        }
        secondary = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "", "descripcion": "Movimiento secundario",
            "etapa": "", "foja": 10,
        }

        forward = _prepare_pjud_movements(
            _make_case(), [primary, secondary], log_undated=False,
        )
        reverse = _prepare_pjud_movements(
            _make_case(), [secondary, primary], log_undated=False,
        )

        forward_keys = {movement["descripcion"]: key for movement, key in forward}
        reverse_keys = {movement["descripcion"]: key for movement, key in reverse}
        assert forward_keys == reverse_keys

    def test_foja_zero_is_not_collapsed_with_missing_foja(self):
        from worker.engine import _prepare_pjud_movements

        base = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Provee",
            "etapa": "Discusión",
        }

        prepared = _prepare_pjud_movements(
            _make_case(),
            [{**base, "foja": 0}, {**base, "foja": None}],
            log_undated=False,
        )

        assert len(prepared) == 2

    def test_sort_digest_matches_cross_runtime_unicode_zero_vector(self):
        from worker.engine import _movement_sort_key

        movement = {
            "folio": 5,
            "fecha": " 2024-05-01 ",
            "cuaderno": "Ｐrincipal",
            "tramite": "Resolución",
            "descripcion": " Árbitro\u00a0  cero ",
            "etapa": "Discusión",
            "foja": 0,
        }

        assert _movement_sort_key(movement)[1] == (
            "f9060484ee6e9a686c3138dcf4d7b90c"
            "d3434490ae8a309a867430284d816d42"
        )

    def test_historical_keys_survive_reordering_and_exact_duplicates(self):
        from worker.engine import _prepare_pjud_movements

        primary = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Primera",
            "etapa": "Discusión", "foja": 10,
        }
        secondary = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "", "descripcion": "Segunda",
            "etapa": "", "foja": 10,
        }
        existing = [
            {
                "external_movement_key": "C-1234-2024:Principal:5",
                "raw_payload": primary,
            },
            {
                "external_movement_key": "C-1234-2024:Principal:5#3",
                "raw_payload": secondary,
            },
        ]

        prepared = _prepare_pjud_movements(
            _make_case(),
            [secondary, primary, dict(primary)],
            log_undated=False,
            existing_movements=existing,
        )

        assert {movement["descripcion"]: key for movement, key in prepared} == {
            "Primera": "C-1234-2024:Principal:5",
            "Segunda": "C-1234-2024:Principal:5#3",
        }

    def test_logical_duplicates_merge_document_metadata_conservatively(self):
        from worker.engine import _prepare_pjud_movements

        incomplete = {
            "folio": None, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Provee", "etapa": "",
            "documento_url": None, "documento_token": None,
            "documentos_adicionales": [{
                "url": "/cert-a", "token": "cert-a-token", "param": "dtaCert",
            }],
            "anexo_func": None, "anexo_token": None,
        }
        complete = {
            **incomplete,
            "documento_url": "/documento",
            "documento_token": "document-token",
            "documento_param": "dtaDoc",
            "documentos_adicionales": [
                {"url": "/cert-a", "token": "cert-a-token", "param": "dtaCert"},
                {"url": "/cert-b", "token": "cert-b-token", "param": "dtaCert"},
            ],
            "anexo_func": "anexoSolicitudCivil",
            "anexo_token": "anexo-token",
        }

        prepared = _prepare_pjud_movements(
            _make_case(), [incomplete, complete], log_undated=True,
        )

        assert len(prepared) == 1
        merged, _ = prepared[0]
        assert merged["documento_url"] == "/documento"
        assert merged["documento_token"] == "document-token"
        assert merged["documento_param"] == "dtaDoc"
        assert merged["documentos_adicionales"] == [
            {"url": "/cert-a", "token": "cert-a-token", "param": "dtaCert"},
            {"url": "/cert-b", "token": "cert-b-token", "param": "dtaCert"},
        ]
        assert merged["anexo_func"] == "anexoSolicitudCivil"
        assert merged["anexo_token"] == "anexo-token"

    @pytest.mark.asyncio
    async def test_document_writeback_uses_insert_key_and_skips_undated_movements(self):
        from app.document_downloader import DownloadedDoc

        engine, _, mock_sb, *_ = _make_engine()
        engine._r2 = AsyncMock()
        engine._r2.exists.return_value = False
        undated = {
            "folio": None, "cuaderno": "Principal", "fecha": None,
            "tramite": "Resolución", "descripcion": "Sin fecha",
            "documento_url": "/undated", "documento_token": "undated-token",
            "anexo_func": "anexoSolicitudCivil", "anexo_token": "undated-anexo",
        }
        dated = {
            "folio": None, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Provee", "etapa": "Discusión",
            "foja": None, "sala": "", "estado": "",
            "documento_url": "/dated", "documento_token": "dated-token",
            "documentos_adicionales": [],
            "anexo_func": "anexoSolicitudCivil", "anexo_token": "dated-anexo",
        }
        detail = {"movements": [undated, dated]}

        await engine._upsert_movements(_make_case(), detail)
        inserted_key = mock_sb.from_.return_value.upsert.call_args[0][0][0]["external_movement_key"]

        mock_sb.reset_mock()
        session = AsyncMock()
        session.fetch_anexo_list.return_value = "<html>anexo</html>"
        primary = DownloadedDoc(0, b"primary", "application/pdf", "pdf")
        anexo = DownloadedDoc(0, b"anexo", "application/pdf", "pdf")
        with patch("worker.engine.download_documents", new=AsyncMock(return_value=[primary])) as downloader, \
             patch("worker.engine.parse_anexo_list", return_value=[{
                 "download_url": "/anexo", "download_token": "anexo-token",
                 "download_param": "dtaDoc", "label": "Anexo", "codigo": "A1",
             }]), \
             patch("worker.engine.download_single_document", new=AsyncMock(return_value=anexo)):
            await engine._download_and_store_documents(_make_case(), detail, session)

        downloaded_movements = downloader.await_args.args[1]
        assert downloaded_movements == [dated]
        external_key_filters = [
            call.args[1]
            for call in mock_sb.from_.return_value.eq.call_args_list
            if call.args and call.args[0] == "external_movement_key"
        ]
        assert external_key_filters == [inserted_key]
        uploaded_keys = [call.args[0] for call in engine._r2.upload.await_args_list]
        assert all(inserted_key in storage_key for storage_key in uploaded_keys)
        assert all("undated" not in storage_key for storage_key in uploaded_keys)

    @pytest.mark.asyncio
    async def test_document_writeback_preserves_historical_suffixed_key(self):
        from app.document_downloader import DownloadedDoc

        engine, _, mock_sb, *_ = _make_engine()
        engine._r2 = AsyncMock()
        engine._r2.exists.return_value = False
        primary = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Primera",
            "etapa": "Discusión", "foja": 10,
            "documento_url": None, "documentos_adicionales": [],
        }
        secondary = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "", "descripcion": "Segunda",
            "etapa": "", "foja": 10,
            "documento_url": "/secondary", "documento_token": "token-secondary",
            "documentos_adicionales": [],
        }
        engine._load_existing_movements = AsyncMock(return_value=[
            {
                "external_movement_key": "C-1234-2024:Principal:5",
                "raw_payload": primary,
            },
            {
                "external_movement_key": "C-1234-2024:Principal:5#3",
                "raw_payload": secondary,
            },
        ])
        detail = {"movements": [secondary, primary]}
        # The incremental downloader receives only movements still missing in
        # R2, so its index is relative to that filtered list.
        downloaded = DownloadedDoc(0, b"secondary", "application/pdf", "pdf")

        with patch(
            "worker.engine.download_documents",
            new=AsyncMock(return_value=[downloaded]),
        ):
            await engine._download_and_store_documents(
                _make_case(), detail, AsyncMock(),
            )

        uploaded_keys = [call.args[0] for call in engine._r2.upload.await_args_list]
        assert uploaded_keys == [
            "firm-uuid-1/case-uuid-1/C-1234-2024:Principal:5#3.pdf",
        ]

    @pytest.mark.asyncio
    async def test_existing_primary_is_checked_before_any_pjud_download(self):
        engine, _, _, *_ = _make_engine()
        engine._r2 = AsyncMock()
        engine._r2.exists.return_value = True
        movement = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Provee",
            "documento_url": "/doc", "documento_token": "fresh-jwt",
            "documentos_adicionales": [],
        }
        storage_key = "firm-uuid-1/case-uuid-1/C-1234-2024:Principal:5.pdf"
        engine._load_existing_movements = AsyncMock(return_value=[{
            "external_movement_key": "C-1234-2024:Principal:5",
            "raw_payload": movement,
            "documents": [{
                "type": "principal", "storage_key": storage_key,
                "content_type": "application/pdf", "label": "Documento",
            }],
        }])

        with patch("worker.engine.download_documents", new=AsyncMock(return_value=[])) as downloader:
            await engine._download_and_store_documents(
                _make_case(), {"movements": [movement]}, AsyncMock(),
            )

        assert downloader.await_args.args[1] == []
        engine._r2.upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_certificate_is_not_downloaded_again_for_new_jwt(self):
        from worker.engine import _document_source_id

        engine, _, _, *_ = _make_engine()
        engine._r2 = AsyncMock()
        engine._r2.exists.return_value = True
        cert = {"url": "/cert", "token": "fresh-jwt", "param": "dtaCert"}
        movement = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Provee",
            "documento_url": None, "documentos_adicionales": [cert],
        }
        engine._load_existing_movements = AsyncMock(return_value=[{
            "external_movement_key": "C-1234-2024:Principal:5",
            "raw_payload": movement,
            "documents": [{
                "type": "certificado",
                "source_id": _document_source_id("certificate", "/cert", "dtaCert", 0),
                "storage_key": "firm-uuid-1/case-uuid-1/C-1234-2024:Principal:5-cert.pdf",
                "content_type": "application/pdf", "label": "Certificado",
            }],
        }])

        with patch("worker.engine.download_documents", new=AsyncMock(return_value=[])), \
             patch("worker.engine.download_single_document", new=AsyncMock()) as single:
            await engine._download_and_store_documents(
                _make_case(), {"movements": [movement]}, AsyncMock(),
            )

        single.assert_not_awaited()
        engine._r2.upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_changed_certificate_identity_at_same_index_is_downloaded(self):
        from app.document_downloader import DownloadedDoc
        from worker.engine import _document_source_id

        engine, _, _, *_ = _make_engine()
        engine._r2 = AsyncMock()
        engine._r2.exists.return_value = True
        cert = {"url": "/cert-new", "token": "fresh-jwt", "param": "dtaCert"}
        movement = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Provee",
            "documento_url": None, "documentos_adicionales": [cert],
        }
        engine._load_existing_movements = AsyncMock(return_value=[{
            "external_movement_key": "C-1234-2024:Principal:5",
            "raw_payload": movement,
            "documents": [{
                "type": "certificado",
                "source_id": _document_source_id("certificate", "/cert-old", "dtaCert", 0),
                "storage_key": "firm-uuid-1/case-uuid-1/C-1234-2024:Principal:5-cert.pdf",
                "content_type": "application/pdf", "label": "Certificado",
            }],
        }])

        with patch("worker.engine.download_documents", new=AsyncMock(return_value=[])), \
             patch(
                 "worker.engine.download_single_document",
                 new=AsyncMock(return_value=DownloadedDoc(
                     0, b"new-certificate", "application/pdf", "pdf",
                 )),
             ) as single:
            await engine._download_and_store_documents(
                _make_case(), {"movements": [movement]}, AsyncMock(),
            )

        single.assert_awaited_once()
        engine._r2.upload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_anexo_downloads_only_new_stable_identity(self):
        from app.document_downloader import DownloadedDoc
        from worker.engine import _document_source_id

        engine, _, _, *_ = _make_engine()
        engine._r2 = AsyncMock()
        engine._r2.exists.return_value = True
        movement = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Provee",
            "documento_url": None, "documentos_adicionales": [],
            "anexo_func": "anexoSolicitudCivil", "anexo_token": "fresh-modal-jwt",
        }
        first = {
            "download_url": "/anexo", "download_token": "fresh-a",
            "download_param": "dtaDoc", "label": "Anexo A", "codigo": "A1",
        }
        second = {
            "download_url": "/anexo", "download_token": "fresh-b",
            "download_param": "dtaDoc", "label": "Anexo B", "codigo": "B1",
        }
        engine._load_existing_movements = AsyncMock(return_value=[{
            "external_movement_key": "C-1234-2024:Principal:5",
            "raw_payload": movement,
            "documents": [{
                "type": "anexo",
                "source_id": _document_source_id(
                    "anexo_document", "/anexo", "dtaDoc", "A1", "Anexo A",
                ),
                "storage_key": "firm-uuid-1/case-uuid-1/C-1234-2024:Principal:5-anexo-0.pdf",
                "content_type": "application/pdf", "label": "Anexo A", "codigo": "A1",
            }],
        }])
        session = AsyncMock()
        session.fetch_anexo_list.return_value = "<html>anexos</html>"

        with patch("worker.engine.download_documents", new=AsyncMock(return_value=[])), \
             patch("worker.engine.parse_anexo_list", return_value=[first, second]), \
             patch(
                 "worker.engine.download_single_document",
                 new=AsyncMock(return_value=DownloadedDoc(
                     0, b"new-anexo", "application/pdf", "pdf",
                 )),
             ) as single:
            await engine._download_and_store_documents(
                _make_case(), {"movements": [movement]}, session,
            )

        single.assert_awaited_once_with(session, "/anexo", "fresh-b", "dtaDoc")
        engine._r2.upload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transient_anexo_list_failure_preserves_historical_document_refs(self):
        engine, _, mock_sb, *_ = _make_engine()
        engine._r2 = AsyncMock()
        engine._r2.exists.return_value = True
        ext_key = "C-1234-2024:Principal:5"
        movement = {
            "folio": 5, "cuaderno": "Principal", "fecha": "2024-05-01",
            "tramite": "Resolución", "descripcion": "Provee",
            "documento_url": "/doc", "documento_token": "jwt",
            "documentos_adicionales": [],
            "anexo_func": "anexoSolicitudCivil", "anexo_token": "modal-jwt",
        }
        historical = [
            {
                "type": "principal", "storage_key": f"firm/case/{ext_key}.pdf",
                "content_type": "application/pdf", "label": "Documento",
            },
            {
                "type": "anexo", "source_id": "a" * 64,
                "storage_key": f"firm/case/{ext_key}-anexo-0.pdf",
                "content_type": "application/pdf", "label": "Anexo histórico",
                "codigo": "A1",
            },
        ]
        engine._load_existing_movements = AsyncMock(return_value=[{
            "id": "movement-1", "external_movement_key": ext_key,
            "raw_payload": movement, "documents": historical,
        }])
        session = AsyncMock()
        session.fetch_anexo_list.side_effect = httpx.ReadError("temporary")

        with patch("worker.engine.download_documents", new=AsyncMock(return_value=[])):
            await engine._download_and_store_documents(
                _make_case(), {"movements": [movement]}, session,
            )

        payloads = [call.args[0] for call in mock_sb.from_.return_value.update.call_args_list]
        writeback = next(payload for payload in payloads if "documents" in payload)
        assert writeback["documents"] == historical

    @pytest.mark.asyncio
    async def test_existing_movement_loader_reads_pages_after_row_thousand(self):
        engine, _, mock_sb, *_ = _make_engine()
        chain = mock_sb.from_.return_value
        first_page = [
            {
                "id": f"movement-{index:04d}",
                "external_movement_key": f"C-1234-2024:Principal:{index}",
                "raw_payload": {"folio": index, "cuaderno": "Principal"},
            }
            for index in range(1_000)
        ]
        historical = {
            "id": "movement-1000",
            "external_movement_key": "C-1234-2024:Principal:5#3",
            "raw_payload": {"folio": 5, "cuaderno": "Principal"},
        }
        chain.execute.side_effect = [
            MagicMock(data=first_page),
            MagicMock(data=[historical]),
        ]

        rows = await engine._load_existing_movements("case-uuid-1")

        assert len(rows) == 1_001
        assert rows[-1] == historical
        assert chain.range.call_args_list == [
            ((0, 999),),
            ((1_000, 1_999),),
        ]

    @pytest.mark.asyncio
    async def test_upsert_movements_manda_claves_unicas(self):
        """El helper puede estar perfecto y no estar CABLEADO: una lista de rows sin
        pasar por _dedupe_movement_keys sigue siendo Python valido y ningun test
        unitario del helper lo nota. Este test mira lo que llega al upsert."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        detail = {"movements": [
            {"folio": 5, "cuaderno": "Principal", "tramite": "Resolucion",
             "descripcion": "primera", "fecha": "01/05/2024", "etapa": ""},
            {"folio": 5, "cuaderno": "Principal", "tramite": "Escrito",
             "descripcion": "segunda", "fecha": "01/05/2024", "etapa": ""},
        ]}
        await engine._upsert_movements(_make_case(), detail)

        rows = mock_sb.from_.return_value.upsert.call_args[0][0]
        keys = [r["external_movement_key"] for r in rows]
        assert len(keys) == 2, "no puede perder movimientos"
        assert len(set(keys)) == 2, f"claves duplicadas llegan al upsert: {keys}"
        assert keys[0] == "C-1234-2024:Principal:5", "la primera conserva su clave"
        assert keys[1] == "C-1234-2024:Principal:5#2"

    @pytest.mark.asyncio
    async def test_same_folio_different_stage_or_foja_are_not_collapsed(self):
        engine, _, mock_sb, *_ = _make_engine()
        first = {
            "folio": 5,
            "cuaderno": "Principal",
            "fecha": "2024-05-01",
            "tramite": "Resolución",
            "descripcion": "Provee",
            "etapa": "Discusión",
            "foja": 10,
            "sala": "Primera",
            "estado": "Pendiente",
        }
        second = {
            **first,
            "etapa": "Cumplimiento",
            "foja": 11,
            "sala": "Segunda",
            "estado": "Firmado",
        }

        await engine._upsert_movements(
            _make_case(), {"movements": [first, second]},
        )

        rows = mock_sb.from_.return_value.upsert.call_args.args[0]
        assert [row["external_movement_key"] for row in rows] == [
            "C-1234-2024:Principal:5",
            "C-1234-2024:Principal:5#2",
        ]

    def test_desambigua_claves_duplicadas_en_el_mismo_batch(self):
        """Dos movimientos con el mismo cuaderno+folio rompian el upsert ENTERO con
        'ON CONFLICT DO UPDATE command cannot affect row a second time' (21000).
        Es lo que dejo T-100-2024 suspendida desde el 13 de marzo."""
        from worker.engine import _dedupe_movement_keys
        keys = _dedupe_movement_keys([
            "T-100-2024:Principal:5",
            "T-100-2024:Principal:5",
            "T-100-2024:Principal:6",
            "T-100-2024:Principal:5",
        ])
        assert keys == [
            "T-100-2024:Principal:5",
            "T-100-2024:Principal:5#2",
            "T-100-2024:Principal:6",
            "T-100-2024:Principal:5#3",
        ]

    def test_la_primera_ocurrencia_conserva_su_clave(self):
        """Si la primera cambiara, las filas ya guardadas dejarian de matchear y se
        reinsertarian como movimientos NUEVOS -> notificaciones falsas en masa."""
        from worker.engine import _dedupe_movement_keys
        assert _dedupe_movement_keys(["a:b:1", "a:b:1"])[0] == "a:b:1"

    def test_no_toca_las_claves_si_no_hay_duplicados(self):
        from worker.engine import _dedupe_movement_keys
        assert _dedupe_movement_keys(["a:b:1", "a:b:2"]) == ["a:b:1", "a:b:2"]

    def test_no_pierde_ningun_movimiento(self):
        """Deduplicar descartando perderia un movimiento de una causa judicial en
        silencio, que es justo lo que este proyecto existe para evitar."""
        from worker.engine import _dedupe_movement_keys
        entrada = ["x:y:1"] * 4
        salida = _dedupe_movement_keys(entrada)
        assert len(salida) == len(entrada)
        assert len(set(salida)) == len(entrada)

    def test_map_tramite_resolution(self):
        from worker.engine import _map_tramite
        assert _map_tramite("Resolución auto") == "resolution"
        assert _map_tramite("Resolucion numero 5") == "resolution"

    def test_map_tramite_filing(self):
        from worker.engine import _map_tramite
        assert _map_tramite("Escrito de parte") == "filing"

    def test_map_tramite_notification(self):
        from worker.engine import _map_tramite
        assert _map_tramite("Actuacion Receptor notifica") == "notification"
        assert _map_tramite("Actuación Receptor diligencia") == "notification"

    def test_map_tramite_other(self):
        from worker.engine import _map_tramite
        assert _map_tramite("Algo desconocido") == "other"


class TestSearchPjudViaSession:
    @pytest.mark.asyncio
    async def test_returns_matches_when_found(self):
        from worker.engine import search_pjud_via_session

        mock_session = AsyncMock()
        # Realistic-length body (real OJV search results pages are always
        # well over 100 chars) so this doesn't trip the G1 contentless guard.
        mock_session.search = AsyncMock(return_value="<html>" + "result " * 20 + "</html>")

        with patch("worker.engine.detect_blocked", return_value=False), \
             patch("worker.engine.parse_search_results", return_value=[{"key": "abc"}]):
            result = await search_pjud_via_session(
                mock_session, "civil", {"action": "search"}, 25.0
            )

        assert result["found"] is True
        assert result["match_count"] == 1
        assert result["blocked"] is False

    @pytest.mark.asyncio
    async def test_returns_blocked_when_detected(self):
        from worker.engine import search_pjud_via_session

        mock_session = AsyncMock()
        mock_session.search = AsyncMock(return_value="<html>captcha</html>")

        with patch("worker.engine.detect_blocked", return_value=True):
            result = await search_pjud_via_session(
                mock_session, "civil", {"action": "search"}, 25.0
            )

        assert result["blocked"] is True
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_returns_not_found_when_no_matches(self):
        from worker.engine import search_pjud_via_session

        mock_session = AsyncMock()
        # Realistic-length body — genuinely "no results" pages from OJV still
        # render a full page shell, they just have 0 result rows. This is
        # distinct from the G1 contentless soft-block case (~39 bytes).
        mock_session.search = AsyncMock(return_value="<html>" + "No se encontraron causas " * 20 + "</html>")

        with patch("worker.engine.detect_blocked", return_value=False), \
             patch("worker.engine.parse_search_results", return_value=[]):
            result = await search_pjud_via_session(
                mock_session, "civil", {"action": "search"}, 25.0
            )

        assert result["found"] is False
        assert result["match_count"] == 0
        assert result["blocked"] is False

    @pytest.mark.asyncio
    async def test_nonempty_unparseable_search_response_is_upstream_changed(self):
        """A PJUD page without its explicit absence marker is not case absence."""
        from app.failure_kind import UpstreamChangedError
        from worker.engine import search_pjud_via_session

        mock_session = AsyncMock()
        mock_session.search = AsyncMock(return_value="<html>" + "unknown markup " * 20 + "</html>")

        with patch("worker.engine.detect_blocked", return_value=False), \
             patch("worker.engine.parse_search_results", return_value=[]):
            with pytest.raises(UpstreamChangedError):
                await search_pjud_via_session(mock_session, "civil", {"action": "search"}, 25.0)

    @pytest.mark.asyncio
    async def test_search_parser_rejection_is_upstream_changed(self):
        """Parser drift is retryable infrastructure, never an invalid cause."""
        from app.failure_kind import UpstreamChangedError
        from worker.engine import search_pjud_via_session

        mock_session = AsyncMock()
        mock_session.search = AsyncMock(return_value="<html>" + "result " * 40 + "</html>")

        with patch("worker.engine.detect_blocked", return_value=False), \
             patch("worker.engine.parse_search_results", side_effect=ValueError("unexpected PJUD table")):
            with pytest.raises(UpstreamChangedError):
                await search_pjud_via_session(mock_session, "civil", {"action": "search"}, 25.0)

    @pytest.mark.asyncio
    async def test_cuerpo_de_cero_bytes_lanza_en_vez_de_reportar_bloqueo(self):
        """La distinción con el test de abajo es de un byte y es deliberada.

        ~39 bytes de esqueleto HTML es un soft-block de F5 medido. Cero bytes es
        la respuesta que no llegó: cargársela a OJV le escribe al abogado
        "bloqueado por OJV" cuando lo que hay que revisar es el proxy residencial.
        Lanza para que `sync_case` la clasifique como infra por el mismo camino
        que un `ProxyError`.
        """
        from app.failure_kind import EmptyResponseError
        from worker.engine import search_pjud_via_session

        mock_session = AsyncMock()
        mock_session.search = AsyncMock(return_value="   ")

        with pytest.raises(EmptyResponseError):
            await search_pjud_via_session(mock_session, "civil", {"action": "search"}, 25.0)

    @pytest.mark.asyncio
    async def test_returns_blocked_on_contentless_soft_block(self):
        """G1: an F5 soft-block returns a NON-empty but contentless page
        (`<html><head></head><body></body></html>`, ~39 bytes). detect_blocked
        returns False for it (no bobcmn marker), but it must still be treated
        as blocked — mirroring the detail path's len<100 guard — instead of
        being parsed into 0 matches and treated as "not found"."""
        from worker.engine import search_pjud_via_session

        soft_block_html = "<html><head></head><body></body></html>"
        assert len(soft_block_html.strip()) < 100

        mock_session = AsyncMock()
        mock_session.search = AsyncMock(return_value=soft_block_html)

        with patch("worker.engine.detect_blocked", return_value=False), \
             patch("worker.engine.parse_search_results") as mock_parse:
            result = await search_pjud_via_session(
                mock_session, "civil", {"action": "search"}, 25.0
            )

        assert result["blocked"] is True
        assert result["found"] is False
        assert result["match_count"] == 0
        mock_parse.assert_not_called()


class TestDetailPjudViaSession:
    @pytest.mark.asyncio
    async def test_returns_parsed_detail_when_valid(self):
        from worker.engine import detail_pjud_via_session

        html = "<html>" + "x" * 200 + "</html>"
        mock_session = AsyncMock()
        mock_session.detail = AsyncMock(return_value=html)

        mock_parsed = {
            "metadata": {"rol": "C-1234-2024"},
            "movements": [],
            "litigantes": [],
        }

        with patch("worker.engine.parse_detail", return_value=mock_parsed):
            result = await detail_pjud_via_session(
                mock_session, "civil", "eyJkey", 25.0
            )

        assert result["blocked"] is False
        assert result["metadata"] == {"rol": "C-1234-2024"}

    @pytest.mark.asyncio
    async def test_detail_cuerpo_de_cero_bytes_lanza(self):
        """Espejo del de `search`: cero bytes es infra, no bloqueo."""
        from app.failure_kind import EmptyResponseError
        from worker.engine import detail_pjud_via_session

        mock_session = AsyncMock()
        mock_session.detail = AsyncMock(return_value="")

        with pytest.raises(EmptyResponseError):
            await detail_pjud_via_session(mock_session, "civil", "eyJkey", 25.0)

    @pytest.mark.asyncio
    async def test_returns_blocked_on_short_response(self):
        from worker.engine import detail_pjud_via_session

        short_html = "<html>err</html>"  # definitely < 100 chars
        mock_session = AsyncMock()
        mock_session.detail = AsyncMock(return_value=short_html)

        result = await detail_pjud_via_session(
            mock_session, "civil", "eyJkey", 25.0
        )

        assert result["blocked"] is True

    @pytest.mark.asyncio
    async def test_detail_detects_f5_challenge(self):
        """A long-enough F5 challenge response at the detail step must be
        detected as blocked, not treated as a valid (empty) success."""
        from worker.engine import detail_pjud_via_session

        challenge_html = (
            '<html><head><script>window["bobcmn"] = "10111...";</script></head><body>'
            + ("x" * 200)
            + "</body></html>"
        )
        mock_session = AsyncMock()
        mock_session.detail = AsyncMock(return_value=challenge_html)

        result = await detail_pjud_via_session(
            mock_session, "civil", "somekey", 25.0
        )

        assert result["blocked"] is True


class TestSyncErrorBackoff:
    @pytest.mark.asyncio
    async def test_sync_error_sets_blocked_until(self):
        """When a case fails with an error, the update should include sync_blocked_until."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(consecutive_sync_failures=0)

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = _mock_search_response(found=False, matches=[])
            result = await engine.sync_case(case)

        assert result["success"] is False

        # Find the update call that sets tracking_status to "error"
        error_update = find_update_payload(mock_sb, tracking_status="error")

        assert error_update is not None, "Expected an update call with tracking_status='error'"
        assert "sync_blocked_until" in error_update, "Error update should set sync_blocked_until"

    @pytest.mark.asyncio
    async def test_sync_error_suspended_after_max_attempts(self):
        """After 10+ CONSECUTIVE failures, the case should be suspended instead of retried."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(consecutive_sync_failures=10)

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = _mock_search_response(found=False, matches=[])
            result = await engine.sync_case(case)

        assert result["success"] is False

        # Find the update call that sets tracking_status to "suspended"
        suspended_update = find_update_payload(mock_sb, tracking_status="suspended")

        assert suspended_update is not None, "Expected an update call with tracking_status='suspended'"
        assert suspended_update["last_sync_status"] == "error"
        assert "Suspended after 10 consecutive failures" in suspended_update["last_sync_error"]
        assert suspended_update["consecutive_sync_failures"] == 11
        # Suspended cases should NOT have sync_blocked_until (they don't retry)
        assert "sync_blocked_until" not in suspended_update

    @pytest.mark.asyncio
    async def test_sync_error_backoff_escalates(self):
        """Higher consecutive_sync_failures should result in longer backoff durations."""
        from worker.engine import SyncEngine, TZ_SANTIAGO
        from datetime import datetime as dt

        backoff_expected = {
            0: 300,     # 5 minutes
            1: 1800,    # 30 minutes
            2: 7200,    # 2 hours
            5: 21600,   # 6 hours (4th+)
        }

        for attempts, expected_seconds in backoff_expected.items():
            engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

            case = _make_case(consecutive_sync_failures=attempts)

            before = dt.now(TZ_SANTIAGO)

            with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
                mock_search.return_value = _mock_search_response(found=False, matches=[])
                await engine.sync_case(case)

            after = dt.now(TZ_SANTIAGO)

            # Find the error update payload
            error_update = find_update_payload(mock_sb, tracking_status="error")

            assert error_update is not None
            blocked_until = dt.fromisoformat(error_update["sync_blocked_until"])
            diff = (blocked_until - before).total_seconds()
            # Allow a small tolerance window (2 seconds)
            assert abs(diff - expected_seconds) < 2, (
                f"For consecutive_sync_failures={attempts}, expected ~{expected_seconds}s backoff, got {diff:.1f}s"
            )
            # Verify consecutive_sync_failures is incremented in the error update
            assert error_update.get("consecutive_sync_failures") == attempts + 1, (
                f"For consecutive_sync_failures={attempts}, expected update to set consecutive_sync_failures={attempts + 1}, "
                f"got {error_update.get('consecutive_sync_failures')}"
            )


_ERROR_PATHS = [
    "identificador_invalido",
    "materia_no_soportada",
    "no_encontrada_en_ojv",
    "excepcion_generica",
]


async def _run_error_path(path: str, failures: int):
    """Lleva `sync_case` a uno de sus caminos de error y devuelve el mock de Supabase.

    Sin rama `else` que absorba lo desconocido: un camino nuevo mal escrito
    tiene que explotar, no caer en el setup de otro y pasar el test contra el
    camino equivocado. Es el mismo default silencioso que motivo este cambio.
    """
    engine, _pool, mock_sb, _notifier, _metrics, _backoff = _make_engine()
    case = _make_case(consecutive_sync_failures=failures)

    if path == "identificador_invalido":
        case["case_number"] = "esto-no-es-un-rol"
        result = await engine.sync_case(case)
    elif path == "materia_no_soportada":
        case["matter"] = "tributario"
        result = await engine.sync_case(case)
    elif path in ("no_encontrada_en_ojv", "excepcion_generica"):
        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
            if path == "no_encontrada_en_ojv":
                mock_search.return_value = _mock_search_response(found=False, matches=[])
            else:
                mock_search.side_effect = RuntimeError("boom")
            result = await engine.sync_case(case)
    else:
        raise AssertionError(f"Camino de error desconocido: {path!r}")

    assert result["success"] is False, f"El camino {path} deberia fallar"
    return mock_sb


class TestElContadorSaleDeLaCausa:
    """El contador que decide backoff y suspension sale de la FILA de la causa.

    `_update_case_error` recibia antes `case["id"]` y el contador como tercer
    parametro con default 0, desde 6 call sites. De esos 6, solo el de "no
    encontrada en OJV" tenia un test que mirara el contador (TestSyncErrorBackoff
    va por ese camino): los otros cinco podian pasar 0 —o no pasar nada— y la
    suite seguia verde. Un 0 ahi es invisible en produccion: la causa reinicia el
    backoff a 5 minutos en cada vuelta y NUNCA llega al umbral de suspension, o
    sea falla para siempre sin que nadie se entere.

    Estos dos tests recorren los cuatro caminos de error de `sync_case` con el
    `_update_case_error` de verdad (sin mock) y miran el payload que sale a
    Supabase, que es donde la invariante se vuelve observable.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _ERROR_PATHS)
    async def test_el_error_incrementa_el_contador_de_la_causa(self, path):
        mock_sb = await _run_error_path(path, failures=7)

        error_update = find_update_payload(mock_sb, tracking_status="error")

        assert error_update is not None, f"El camino {path} no escribio un update de error"
        assert error_update["consecutive_sync_failures"] == 8, (
            f"El camino {path} escribio consecutive_sync_failures="
            f"{error_update['consecutive_sync_failures']}; con la causa en 7 tiene que ser 8. "
            f"Un 0 aca significa que el contador no salio de la causa."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _ERROR_PATHS)
    async def test_en_el_umbral_cualquier_camino_suspende(self, path):
        mock_sb = await _run_error_path(path, failures=10)

        assert find_update_payload(mock_sb, tracking_status="error") is None, (
            f"El camino {path} aplico backoff en vez de suspender: la causa venia con "
            f"10 fallas consecutivas, que es el umbral."
        )
        suspended = find_update_payload(mock_sb, tracking_status="suspended")
        assert suspended is not None, f"El camino {path} no suspendio la causa"
        assert suspended["consecutive_sync_failures"] == 11
