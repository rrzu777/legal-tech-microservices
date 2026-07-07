"""Tests for API session pool health tracking."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()


def _make_mock_session(age=0):
    session = MagicMock()
    session.age_seconds = age
    session.close = AsyncMock()
    return session


class TestAPISessionPool:
    @pytest.mark.asyncio
    async def test_release_healthy_returns_to_pool(self):
        from app.session_pool import APISessionPool
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session, healthy=True)

        assert len(pool._pool) == 1
        session.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_unhealthy_closes_session(self):
        from app.session_pool import APISessionPool
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session, healthy=False)

        assert len(pool._pool) == 0
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_default_is_healthy(self):
        from app.session_pool import APISessionPool
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session)

        assert len(pool._pool) == 1


class TestWorkerSessionPoolAcquire:
    @pytest.mark.asyncio
    async def test_acquire_returns_stale_session_when_refresh_fails(self):
        """A mint/refresh failure during acquire() must NOT propagate — the
        stale (expired-cookie) session should be returned instead. The F5
        challenge it produces is detected downstream and routed through the
        no-penalty blocked path (see engine._handle_blocked / detect_blocked),
        which keeps the anti-outage invariant: mint failures never reach
        _update_case_error / sync_attempts.

        NOTE: as of the N-slot checkout pool (Task 5a), the worker pool is
        slot-based (`_slots`, `_refresh_slot`) rather than a flat `_pool` of
        sessions. This test targets the same behavior against the new model.
        See also tests/test_session_pool_proxy.py for the fuller slot-pool
        coverage (distinct IPs, checkout distinctness, cooldown, etc.)."""
        from worker.session_pool import SessionPool, _Slot

        config = MagicMock()
        config.COOKIE_STORE_PATH = "/tmp/x.json"
        config.PJUD_BASE_URL = "https://x"
        config.RATE_LIMIT_MS = 0
        config.SESSION_MAX_AGE_S = 1500
        config.POOL_SIZE = 1
        config.OJV_PROXY_URL = None
        config.OJV_PROXY_STICKY_LIFETIME = "1h"
        config.OJV_PROXY_POOL_SIZE = 3
        config.BLOCK_PAUSE_S = 30

        pool = SessionPool(config)
        old = MagicMock()
        old.age_seconds = 999999  # forces refresh
        slot = _Slot(index=0, session=old)
        pool._slots = [slot]
        pool._refresh_slot = AsyncMock(side_effect=RuntimeError("mint failed"))

        result = await pool.acquire()

        assert result is old  # stale session returned, NOT raised
        await pool.release(result)  # must not raise; semaphore consistent
