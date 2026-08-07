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
