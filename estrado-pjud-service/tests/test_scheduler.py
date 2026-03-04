import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


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
        chain.eq.return_value = chain
        chain.or_.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        scheduler = Scheduler(_mock_config(), mock_sb)
        result = await scheduler.get_next_batch()

        assert result == []
        mock_sb.from_.assert_called_with("cases")
        chain.select.assert_called_once_with("*")

    @pytest.mark.asyncio
    async def test_filters_by_priority_during_office_hours(self):
        from worker.scheduler import Scheduler, _is_office_hours
        from zoneinfo import ZoneInfo

        dt_office = datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_office_hours(dt_office) is True

        dt_night = datetime(2026, 3, 2, 22, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_office_hours(dt_night) is False

    @pytest.mark.asyncio
    async def test_archived_only_on_sunday_night(self):
        from worker.scheduler import _is_archived_window
        from zoneinfo import ZoneInfo

        # Sunday 23:00 -> allowed
        dt_sun = datetime(2026, 3, 1, 23, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_archived_window(dt_sun) is True

        # Sunday 03:00 -> allowed
        dt_sun_early = datetime(2026, 3, 1, 3, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_archived_window(dt_sun_early) is True

        # Monday 10:00 -> not allowed
        dt_mon = datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_archived_window(dt_mon) is False

    @pytest.mark.asyncio
    async def test_marks_batch_with_worker_id(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.or_.return_value = chain
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
        result = await scheduler.get_next_batch()

        assert len(result) == 2
