from unittest.mock import AsyncMock, MagicMock
import pytest


def _make_engine(pool):
    from worker.engine import SyncEngine
    return SyncEngine(
        pool=pool, supabase=MagicMock(), notifier=MagicMock(),
        metrics=MagicMock(), backoff=MagicMock(),
        config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
    )


@pytest.mark.asyncio
async def test_blocked_does_not_increment_sync_attempts():
    pool = MagicMock()
    pool.force_remint = AsyncMock()
    engine = _make_engine(pool)
    engine._update_case_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()

    await engine._handle_blocked("c1")

    engine._update_case_blocked.assert_awaited_once_with("c1")
    engine._update_case_error.assert_not_awaited()
    pool.force_remint.assert_awaited_once()
    engine._backoff.record_blocked.assert_called_once()


@pytest.mark.asyncio
async def test_blocked_survives_remint_failure():
    pool = MagicMock()
    pool.force_remint = AsyncMock(side_effect=RuntimeError("mint failed"))
    engine = _make_engine(pool)
    engine._update_case_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()

    # Must not raise even if the re-mint fails
    await engine._handle_blocked("c1")

    engine._update_case_error.assert_not_awaited()
    engine._backoff.record_blocked.assert_called_once()
