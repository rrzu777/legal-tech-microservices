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
    engine = _make_engine(pool)
    engine._update_case_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()

    await engine._handle_blocked("c1")

    engine._update_case_blocked.assert_awaited_once_with("c1")
    engine._update_case_error.assert_not_awaited()
    engine._backoff.record_blocked.assert_called_once()


@pytest.mark.asyncio
async def test_blocked_does_not_touch_the_pool():
    """Regression guard: _handle_blocked must NOT call any pool method. The
    re-mint responsibility moved entirely to sync_case's release(healthy=False)
    (owned by the caller that saw the block). This anchors the anti-outage
    contract now that the old force_remint escalation path is gone: block
    handling stays a pure state transition (mark blocked + open breaker)."""
    pool = MagicMock()
    engine = _make_engine(pool)
    engine._update_case_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()

    await engine._handle_blocked("c1")

    # No pool interaction whatsoever (acquire/release/refresh/etc.).
    pool.assert_not_called()
    assert pool.method_calls == []
    engine._update_case_error.assert_not_awaited()
    engine._backoff.record_blocked.assert_called_once()
