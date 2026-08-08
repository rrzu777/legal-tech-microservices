"""Tests for API session pool health tracking."""
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, Mock

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

        assert pool._pool.qsize() == 1
        session.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_unhealthy_closes_session(self):
        from app.session_pool import APISessionPool
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session, healthy=False)

        assert pool._pool.qsize() == 0
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_default_is_healthy(self):
        from app.session_pool import APISessionPool
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session)

        assert pool._pool.qsize() == 1

    @pytest.mark.asyncio
    async def test_try_acquire_ready_never_mints_or_loads_store(self, monkeypatch):
        """A cold opportunistic checkout must stay local and return immediately."""
        from app.config import Settings
        from app.session_pool import APISessionPool

        pool = APISessionPool(Settings(API_KEY="test", _env_file=None))
        mint = AsyncMock(side_effect=AssertionError("must not mint"))
        load = Mock(side_effect=AssertionError("must not load bundles"))
        monkeypatch.setattr(pool, "_mint_on_demand", mint)
        monkeypatch.setattr(pool._store, "load_all", load)

        assert await pool.try_acquire_ready() is None
        mint.assert_not_awaited()
        load.assert_not_called()

    @pytest.mark.asyncio
    async def test_try_acquire_ready_returns_only_existing_valid_session(self):
        from app.config import Settings
        from app.session_pool import APISessionPool

        pool = APISessionPool(Settings(API_KEY="test", _env_file=None))
        ready_session = _make_mock_session(age=10)
        await pool.release(ready_session)

        assert await pool.try_acquire_ready() is ready_session
        assert pool._pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_try_acquire_ready_does_not_wait_for_pool_lock(self):
        """A busy interactive checkout must make opportunistic work skip, not wait."""
        from app.config import Settings
        from app.session_pool import APISessionPool

        pool = APISessionPool(Settings(API_KEY="test", _env_file=None))
        ready = _make_mock_session(age=10)
        await pool.release(ready)
        await pool._lock.acquire()
        try:
            result = await asyncio.wait_for(pool.try_acquire_ready(), timeout=0.01)
        finally:
            pool._lock.release()

        assert result is None
        assert pool._pool.get_nowait() is ready

    @pytest.mark.asyncio
    async def test_try_acquire_ready_discards_expired_entries_without_blocking(self):
        from app.config import Settings
        from app.session_pool import APISessionPool

        pool = APISessionPool(Settings(API_KEY="test", _env_file=None))
        expired = _make_mock_session(age=pool._max_age)
        ready = _make_mock_session(age=10)
        pool._pool.put_nowait(expired)
        pool._pool.put_nowait(ready)

        assert await pool.try_acquire_ready() is ready
        await pool.close_all()
        expired.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_try_acquire_ready_does_not_wait_for_expired_session_close(self):
        """Cleanup of stale entries cannot delay opportunistic acquisition."""
        from app.config import Settings
        from app.session_pool import APISessionPool

        pool = APISessionPool(Settings(API_KEY="test", _env_file=None))
        close_started = asyncio.Event()
        permit_close = asyncio.Event()

        async def slow_close():
            close_started.set()
            await permit_close.wait()

        expired = _make_mock_session(age=pool._max_age)
        expired.close = AsyncMock(side_effect=slow_close)
        ready = _make_mock_session(age=10)
        pool._pool.put_nowait(expired)
        pool._pool.put_nowait(ready)

        try:
            assert await asyncio.wait_for(pool.try_acquire_ready(), timeout=0.01) is ready
            await asyncio.wait_for(close_started.wait(), timeout=0.01)
        finally:
            permit_close.set()
            await pool.close_all()
        assert not pool._closing_tasks

    @pytest.mark.asyncio
    async def test_expired_background_close_exception_is_consumed(self, caplog):
        from app.config import Settings
        from app.session_pool import APISessionPool

        pool = APISessionPool(Settings(API_KEY="test", _env_file=None))
        expired = _make_mock_session(age=pool._max_age)
        expired.close = AsyncMock(side_effect=RuntimeError("close failed"))
        pool._pool.put_nowait(expired)

        assert await pool.try_acquire_ready() is None
        await pool.close_all()

        assert not pool._closing_tasks
        assert "Failed to close expired ready-only API session" in caplog.text


def test_ojv_session_generation_id_is_random_and_can_be_injected():
    from app.session import OJVSession

    adapter = MagicMock()
    first = OJVSession(adapter)
    second = OJVSession(adapter)
    supplied = uuid.UUID("11111111-1111-4111-8111-111111111111")

    assert isinstance(first.generation_id, uuid.UUID)
    assert first.generation_id != second.generation_id
    assert OJVSession(adapter, generation_id=supplied).generation_id == supplied


class TestWorkerSessionPoolAcquire:
    @pytest.mark.asyncio
    async def test_acquire_returns_stale_session_when_refresh_fails(self):
        """A mint/refresh failure during acquire() must NOT propagate — the
        stale (expired-cookie) session should be returned instead. The F5
        challenge it produces is detected downstream and routed through the
        no-penalty blocked path (see engine._handle_blocked / detect_blocked),
        which keeps the anti-outage invariant: mint failures never reach
        _update_case_error / consecutive_sync_failures.

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
        config.MINT_MAX_RETRIES = 3

        pool = SessionPool(config)
        old = MagicMock()
        old.age_seconds = 999999  # forces refresh
        slot = _Slot(index=0, session=old)
        pool._slots = [slot]
        pool._refresh_slot = AsyncMock(side_effect=RuntimeError("mint failed"))

        result = await pool.acquire()

        assert result is old  # stale session returned, NOT raised
        await pool.release(result)  # must not raise; semaphore consistent
