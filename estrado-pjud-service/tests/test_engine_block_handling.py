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
    """_handle_blocked marks the case as blocked and opens the circuit
    breaker, without penalizing sync_attempts. It no longer touches the
    pool directly — per-slot re-mint now happens reactively in sync_case's
    finally via release(session, healthy=False), owned by the caller that
    saw the block, not by _handle_blocked itself."""
    pool = MagicMock()
    pool.force_remint = AsyncMock()
    engine = _make_engine(pool)
    engine._update_case_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()

    await engine._handle_blocked("c1")

    engine._update_case_blocked.assert_awaited_once_with("c1")
    engine._update_case_error.assert_not_awaited()
    pool.force_remint.assert_not_called()
    engine._backoff.record_blocked.assert_called_once()


@pytest.mark.asyncio
async def test_blocked_does_not_raise_when_pool_has_no_remint_path():
    """_handle_blocked must not depend on (or fail because of) any pool
    re-mint mechanism — the re-mint responsibility moved to sync_case's
    release(healthy=False). A pool without a working force_remint (or one
    that would raise) must not affect _handle_blocked's own behavior."""
    pool = MagicMock()
    pool.force_remint = AsyncMock(side_effect=RuntimeError("mint failed"))
    engine = _make_engine(pool)
    engine._update_case_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()

    # Must not raise — _handle_blocked never calls force_remint anymore.
    await engine._handle_blocked("c1")

    engine._update_case_error.assert_not_awaited()
    engine._backoff.record_blocked.assert_called_once()
    pool.force_remint.assert_not_called()
