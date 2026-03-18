# tests/test_engine.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


def _make_case(**overrides):
    base = {
        "id": "case-uuid-1",
        "law_firm_id": "firm-uuid-1",
        "case_number": "C-1234-2024",
        "case_type": "rol",
        "matter": "civil",
        "status": "active",
        "assigned_user_id": "user-uuid-1",
        "sync_attempts": 3,
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
        mock_pool.release = MagicMock()
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
        mock_pool.release = MagicMock()
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
        mock_pool.release.assert_called_once()
        mock_backoff.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_blocked_triggers_backoff(self):
        from worker.engine import SyncEngine

        mock_session = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_session)
        mock_pool.release = MagicMock()
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

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = _mock_search_response(blocked=True)
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_invalid_identifier(self):
        from worker.engine import SyncEngine

        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=AsyncMock())
        mock_pool.release = MagicMock()
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

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response(blocked=True)
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_called_once()
        mock_metrics.record_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_timeout_triggers_failure_backoff(self):
        """Timeout should trigger record_failure on backoff, not record_blocked."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case()

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = TimeoutError("timed out")
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_failure.assert_called_once()
        mock_backoff.record_blocked.assert_not_called()
        mock_metrics.record_error.assert_called_once()

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
        mock_pool.release = MagicMock()
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
        # Session must be released in finally block
        mock_pool.release.assert_called_once_with(mock_session)

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
        mock_session.search = AsyncMock(return_value="<html>result</html>")

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
        mock_session.search = AsyncMock(return_value="<html>empty</html>")

        with patch("worker.engine.detect_blocked", return_value=False), \
             patch("worker.engine.parse_search_results", return_value=[]):
            result = await search_pjud_via_session(
                mock_session, "civil", {"action": "search"}, 25.0
            )

        assert result["found"] is False
        assert result["match_count"] == 0


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
    async def test_returns_blocked_on_short_response(self):
        from worker.engine import detail_pjud_via_session

        short_html = "<html>err</html>"  # definitely < 100 chars
        mock_session = AsyncMock()
        mock_session.detail = AsyncMock(return_value=short_html)

        result = await detail_pjud_via_session(
            mock_session, "civil", "eyJkey", 25.0
        )

        assert result["blocked"] is True


class TestSyncErrorBackoff:
    @pytest.mark.asyncio
    async def test_sync_error_sets_blocked_until(self):
        """When a case fails with an error, the update should include sync_blocked_until."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(sync_attempts=0)

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = _mock_search_response(found=False, matches=[])
            result = await engine.sync_case(case)

        assert result["success"] is False

        # Find the update call that sets tracking_status to "error"
        update_calls = mock_sb.from_.return_value.update.call_args_list
        error_update = None
        for call in update_calls:
            args = call[0] if call[0] else ()
            kwargs = call[1] if call[1] else {}
            payload = args[0] if args else kwargs.get("data")
            if payload and payload.get("tracking_status") == "error":
                error_update = payload
                break

        assert error_update is not None, "Expected an update call with tracking_status='error'"
        assert "sync_blocked_until" in error_update, "Error update should set sync_blocked_until"

    @pytest.mark.asyncio
    async def test_sync_error_suspended_after_max_attempts(self):
        """After 10+ failed attempts, the case should be suspended instead of retried."""
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(sync_attempts=10)

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = _mock_search_response(found=False, matches=[])
            result = await engine.sync_case(case)

        assert result["success"] is False

        # Find the update call that sets tracking_status to "suspended"
        update_calls = mock_sb.from_.return_value.update.call_args_list
        suspended_update = None
        for call in update_calls:
            args = call[0] if call[0] else ()
            payload = args[0] if args else {}
            if payload and payload.get("tracking_status") == "suspended":
                suspended_update = payload
                break

        assert suspended_update is not None, "Expected an update call with tracking_status='suspended'"
        assert suspended_update["last_sync_status"] == "error"
        assert "Suspended after 10 failed attempts" in suspended_update["last_sync_error"]
        assert suspended_update["sync_attempts"] == 11
        # Suspended cases should NOT have sync_blocked_until (they don't retry)
        assert "sync_blocked_until" not in suspended_update

    @pytest.mark.asyncio
    async def test_sync_error_backoff_escalates(self):
        """Higher sync_attempts should result in longer backoff durations."""
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

            case = _make_case(sync_attempts=attempts)

            before = dt.now(TZ_SANTIAGO)

            with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search:
                mock_search.return_value = _mock_search_response(found=False, matches=[])
                await engine.sync_case(case)

            after = dt.now(TZ_SANTIAGO)

            # Find the error update payload
            update_calls = mock_sb.from_.return_value.update.call_args_list
            error_update = None
            for call in update_calls:
                args = call[0] if call[0] else ()
                payload = args[0] if args else {}
                if payload and payload.get("tracking_status") == "error":
                    error_update = payload
                    break

            assert error_update is not None
            blocked_until = dt.fromisoformat(error_update["sync_blocked_until"])
            diff = (blocked_until - before).total_seconds()
            # Allow a small tolerance window (2 seconds)
            assert abs(diff - expected_seconds) < 2, (
                f"For sync_attempts={attempts}, expected ~{expected_seconds}s backoff, got {diff:.1f}s"
            )
            # Verify sync_attempts is incremented in the error update
            assert error_update.get("sync_attempts") == attempts + 1, (
                f"For sync_attempts={attempts}, expected update to set sync_attempts={attempts + 1}, "
                f"got {error_update.get('sync_attempts')}"
            )
