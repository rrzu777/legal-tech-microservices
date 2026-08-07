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
    async def test_get_next_batch_builds_correct_query(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.select.return_value = chain
        chain.in_.return_value = chain
        chain.eq.return_value = chain
        chain.or_.return_value = chain
        chain.lte.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        scheduler = Scheduler(_mock_config(), mock_sb)
        result = await scheduler.get_next_batch(now=OFFICE_NOW)

        assert result == []
        mock_sb.from_.assert_called_with("cases")
        chain.select.assert_called_once_with("*")

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
        mock_sb.from_.assert_not_called()

    @pytest.mark.asyncio
    async def test_marks_batch_with_worker_id(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.select.return_value = chain
        chain.in_.return_value = chain
        chain.eq.return_value = chain
        chain.or_.return_value = chain
        chain.lte.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain

        fake_cases = [{"id": "case-1"}, {"id": "case-2"}]
        chain.execute.return_value = MagicMock(data=fake_cases)

        update_chain = MagicMock()
        chain.update.return_value = update_chain
        update_chain.in_.return_value = update_chain
        update_chain.execute.return_value = MagicMock(data=[])

        config = _mock_config()
        scheduler = Scheduler(config, mock_sb)
        result = await scheduler.get_next_batch(now=OFFICE_NOW)

        assert len(result) == 2
        chain.lte.assert_called_once_with("sync_priority", 3)
