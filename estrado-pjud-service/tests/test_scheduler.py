import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo


OFFICE_NOW = datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("America/Santiago"))
NIGHT_NOW = datetime(2026, 3, 2, 22, 0, tzinfo=ZoneInfo("America/Santiago"))


def _mock_config(worker_id="test-worker", batch_size=10):
    config = MagicMock()
    config.WORKER_ID = worker_id
    config.BATCH_SIZE = batch_size
    config.PJUD_OFF_HOURS_VALIDATION_ONCE = False
    config.PJUD_PROCESS_OUTSIDE_OFFICE_HOURS = False
    return config


class TestScheduler:
    @pytest.mark.asyncio
    async def test_preflight_verifies_rpc_without_claiming_cases(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        await Scheduler(_mock_config(), mock_sb).verify_claim_contract(now=OFFICE_NOW)

        mock_sb.rpc.assert_called_once_with("claim_pjud_sync_cases", {
            "p_worker_id": "test-worker",
            "p_limit": 0,
            "p_now": OFFICE_NOW.isoformat(),
        })

    @pytest.mark.asyncio
    async def test_get_next_batch_builds_correct_query(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        scheduler = Scheduler(_mock_config(), mock_sb)
        result = await scheduler.get_next_batch(now=OFFICE_NOW)

        assert result == []
        mock_sb.rpc.assert_called_once_with("claim_pjud_sync_cases", {
            "p_worker_id": "test-worker",
            "p_limit": 10,
            "p_now": OFFICE_NOW.isoformat(),
        })

    @pytest.mark.asyncio
    async def test_filters_by_priority_during_office_hours(self):
        from worker.scheduler import is_scheduled_processing_window

        dt_office = datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert is_scheduled_processing_window(dt_office) is True

        dt_night = datetime(2026, 3, 2, 22, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert is_scheduled_processing_window(dt_night) is False

    @pytest.mark.asyncio
    async def test_outside_office_returns_before_querying_or_claiming(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        scheduler = Scheduler(_mock_config(), mock_sb)

        assert await scheduler.get_next_batch(now=NIGHT_NOW) == []
        mock_sb.rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_shot_validation_claims_at_most_one_case_outside_office(self):
        from worker.scheduler import Scheduler

        config = _mock_config(batch_size=10)
        config.PJUD_OFF_HOURS_VALIDATION_ONCE = True
        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[{"id": "case-1"}])

        assert await Scheduler(config, mock_sb).get_next_batch(now=NIGHT_NOW) == [
            {"id": "case-1"},
        ]
        mock_sb.rpc.assert_called_once_with("claim_pjud_sync_cases", {
            "p_worker_id": "test-worker",
            "p_limit": 1,
            "p_now": NIGHT_NOW.isoformat(),
        })

    @pytest.mark.asyncio
    async def test_temporary_override_claims_normal_batch_outside_office(self):
        from worker.scheduler import Scheduler

        config = _mock_config(batch_size=10)
        config.PJUD_PROCESS_OUTSIDE_OFFICE_HOURS = True
        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[{"id": "case-1"}])

        assert await Scheduler(config, mock_sb).get_next_batch(now=NIGHT_NOW) == [
            {"id": "case-1"},
        ]
        mock_sb.rpc.assert_called_once_with("claim_pjud_sync_cases", {
            "p_worker_id": "test-worker",
            "p_limit": 10,
            "p_now": NIGHT_NOW.isoformat(),
        })

    @pytest.mark.asyncio
    async def test_marks_batch_with_worker_id(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain

        fake_cases = [{"id": "case-1"}, {"id": "case-2"}]
        chain.execute.return_value = MagicMock(data=fake_cases)

        config = _mock_config()
        scheduler = Scheduler(config, mock_sb)
        result = await scheduler.get_next_batch(now=OFFICE_NOW)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_only_rows_won_by_atomic_claim(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[{"id": "case-2"}])

        result = await Scheduler(_mock_config(), mock_sb).get_next_batch(now=OFFICE_NOW)

        assert result == [{"id": "case-2"}]

    def test_aware_datetime_is_converted_to_santiago(self):
        from worker.scheduler import is_scheduled_processing_window

        # 19:00 UTC = 15:00 Santiago en agosto; evaluar 19:00 sin convertir
        # daría falsamente fuera de horario.
        assert is_scheduled_processing_window(
            datetime(2026, 8, 6, 19, 0, tzinfo=timezone.utc)
        ) is True

    @pytest.mark.asyncio
    async def test_release_prefers_fenced_rpc(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        rpc_query = MagicMock()
        mock_sb.rpc.return_value = rpc_query
        rpc_query.execute.return_value = MagicMock(data=["case-1"])

        await Scheduler(_mock_config(), mock_sb).release_batch(["case-1"])

        mock_sb.rpc.assert_called_once_with("release_pjud_sync_claims", {
            "p_worker_id": "test-worker",
            "p_case_ids": ["case-1"],
        })
        mock_sb.from_.assert_not_called()

    @pytest.mark.asyncio
    async def test_release_falls_back_to_cas_update_before_rpc_migration(self):
        from worker.scheduler import Scheduler

        missing_rpc = RuntimeError({"code": "PGRST202", "message": "not found"})
        mock_sb = MagicMock()
        rpc_query = MagicMock()
        direct_query = MagicMock()
        mock_sb.rpc.return_value = rpc_query
        mock_sb.from_.return_value.update.return_value.in_.return_value.eq.return_value = direct_query
        rpc_query.execute.side_effect = missing_rpc
        direct_query.execute.return_value = MagicMock(data=[{"id": "case-1"}])

        await Scheduler(_mock_config(), mock_sb).release_batch(["case-1"])

        mock_sb.from_.return_value.update.assert_called_once_with({
            "sync_worker_id": None,
            "sync_claimed_at": None,
        })
        mock_sb.from_.return_value.update.return_value.in_.assert_called_once_with(
            "id", ["case-1"]
        )
        mock_sb.from_.return_value.update.return_value.in_.return_value.eq.assert_called_once_with(
            "sync_worker_id", "test-worker"
        )

    @pytest.mark.asyncio
    async def test_release_retries_rpc_if_migration_lands_before_direct_fallback(self):
        from worker.scheduler import Scheduler

        missing_rpc = RuntimeError({"code": "PGRST202", "message": "not found"})
        trigger_rejected = RuntimeError({"code": "42501", "message": "fenced"})
        mock_sb = MagicMock()
        first_rpc = MagicMock()
        stale_cache_rpc = MagicMock()
        second_stale_cache_rpc = MagicMock()
        refreshed_rpc = MagicMock()
        direct_query = MagicMock()
        mock_sb.rpc.side_effect = [
            first_rpc,
            stale_cache_rpc,
            second_stale_cache_rpc,
            refreshed_rpc,
        ]
        mock_sb.from_.return_value.update.return_value.in_.return_value.eq.return_value = direct_query
        first_rpc.execute.side_effect = missing_rpc
        direct_query.execute.side_effect = trigger_rejected
        stale_cache_rpc.execute.side_effect = missing_rpc
        second_stale_cache_rpc.execute.side_effect = missing_rpc
        refreshed_rpc.execute.return_value = MagicMock(data=["case-1"])

        with patch("worker.scheduler.asyncio.sleep", new=AsyncMock()) as sleep:
            await Scheduler(_mock_config(), mock_sb).release_batch(["case-1"])

        assert mock_sb.rpc.call_count == 4
        assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]

    @pytest.mark.asyncio
    async def test_release_preserves_direct_error_after_schema_cache_retry_exhaustion(self):
        from worker.scheduler import RELEASE_SCHEMA_CACHE_RETRY_DELAYS, Scheduler

        missing_rpc = RuntimeError({"code": "PGRST202", "message": "not found"})
        trigger_rejected = RuntimeError({"code": "42501", "message": "fenced"})
        mock_sb = MagicMock()
        mock_sb.rpc.return_value.execute.side_effect = missing_rpc
        mock_sb.from_.return_value.update.return_value.in_.return_value.eq.return_value.execute.side_effect = trigger_rejected

        with patch("worker.scheduler.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(RuntimeError, match="fenced"):
                await Scheduler(_mock_config(), mock_sb).release_batch(["case-1"])

        assert mock_sb.rpc.call_count == 2 + len(RELEASE_SCHEMA_CACHE_RETRY_DELAYS)
        assert sleep.await_count == len(RELEASE_SCHEMA_CACHE_RETRY_DELAYS)

    @pytest.mark.asyncio
    async def test_release_does_not_hide_non_missing_rpc_failure(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        mock_sb.rpc.return_value.execute.side_effect = RuntimeError(
            {"code": "PGRST301", "message": "database unavailable"}
        )

        with pytest.raises(RuntimeError, match="database unavailable"):
            await Scheduler(_mock_config(), mock_sb).release_batch(["case-1"])

        mock_sb.from_.assert_not_called()
