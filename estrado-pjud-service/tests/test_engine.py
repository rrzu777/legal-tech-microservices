# tests/test_engine.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

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
        """G2: a timeout is an INFRA failure (bad/slow residential IP), not the
        case's fault. It must be treated like a block: _handle_blocked (which
        calls record_blocked, NOT record_failure), no _update_case_error, no
        consecutive_sync_failures increment, and release(healthy=False) so the slot
        re-mints."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()
        mock_session = mock_pool.acquire.return_value

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
            mock_search.side_effect = TimeoutError("timed out")
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_called_once()
        mock_backoff.record_failure.assert_not_called()
        mock_metrics.record_error.assert_called_once()
        mock_update_error.assert_not_called()
        mock_pool.release.assert_awaited_once_with(mock_session, healthy=False)
        update_calls = mock_sb.from_.return_value.update.call_args_list
        for call in update_calls:
            payload = call[0][0] if call[0] else {}
            assert "consecutive_sync_failures" not in payload

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
        mock_backoff.record_blocked.assert_called_once()
        mock_update_error.assert_not_called()
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
        chain.upsert.return_value = chain
        chain.in_.return_value = chain

        # Return 0 for before-count, 2 for after-count
        execute_returns = [
            MagicMock(data={"id": "sync-run-1"}, count=None),  # sync run insert
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

    def test_compute_priority_recent_movement(self):
        from worker.engine import _compute_priority
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=3)).isoformat()
        assert _compute_priority("active", recent) == 1

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
        mock_session.search = AsyncMock(return_value="<html>" + "no results here " * 20 + "</html>")

        with patch("worker.engine.detect_blocked", return_value=False), \
             patch("worker.engine.parse_search_results", return_value=[]):
            result = await search_pjud_via_session(
                mock_session, "civil", {"action": "search"}, 25.0
            )

        assert result["found"] is False
        assert result["match_count"] == 0
        assert result["blocked"] is False

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
