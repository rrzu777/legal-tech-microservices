"""Tests for API session pool health tracking."""
import asyncio
import uuid
from contextlib import asynccontextmanager
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
    @staticmethod
    def _proxy_pool_with_captured_usage():
        from app.bandwidth import ProxyUsageCapture
        from app.config import Settings
        from app.session_pool import APISessionPool

        calls = []
        tracker = MagicMock()

        @asynccontextmanager
        async def track(**kwargs):
            calls.append(kwargs)
            usage = ProxyUsageCapture()
            try:
                yield usage
            finally:
                if usage.cause_operation is not None:
                    kwargs["cause_operation"] = usage.cause_operation
                if usage.cause_session_id is not None:
                    kwargs["cause_session_id"] = usage.cause_session_id
                    usage.causal_event_persisted = True

        tracker.track.side_effect = track
        settings = Settings(
            API_KEY="test",
            OJV_PROXY_URL="http://proxy.test:1234",
            _env_file=None,
        )
        return APISessionPool(
            settings,
            allow_uncontrolled_proxy=True,
            proxy_usage=tracker,
        ), calls

    @pytest.mark.asyncio
    async def test_release_healthy_returns_to_pool(self):
        from app.session_pool import APISessionPool, SessionReleaseOutcome
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        outcome = await pool.release(session, healthy=True)

        assert outcome == SessionReleaseOutcome(requeued=True, retired_reason=None)
        assert pool._pool.qsize() == 1
        session.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_unhealthy_closes_session(self):
        from app.session_pool import APISessionPool, SessionReleaseOutcome
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        outcome = await pool.release(session, healthy=False)

        assert outcome == SessionReleaseOutcome(
            requeued=False,
            retired_reason="unhealthy",
        )
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
    @pytest.mark.parametrize("pool_size", [0, -1])
    async def test_release_nonpositive_pool_size_closes_without_retaining(
        self,
        pool_size,
    ):
        """Nonpositive sizes preserve the old disabled-retention behavior."""
        from app.config import Settings
        from app.session_pool import APISessionPool, SessionReleaseOutcome

        settings = Settings(API_KEY="test", _env_file=None)
        settings.SESSION_POOL_SIZE = pool_size
        pool = APISessionPool(settings)
        session = _make_mock_session(age=10)

        outcome = await pool.release(session)

        assert outcome == SessionReleaseOutcome(
            requeued=False,
            retired_reason="disabled",
        )
        assert pool._pool.qsize() == 0
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_reports_expired_and_full_retirement_reasons(self):
        from app.config import Settings
        from app.session_pool import APISessionPool, SessionReleaseOutcome

        pool = APISessionPool(Settings(API_KEY="test", _env_file=None))
        expired = _make_mock_session(age=pool._max_age)
        assert await pool.release(expired) == SessionReleaseOutcome(
            requeued=False,
            retired_reason="expired",
        )

        pool._pool.put_nowait(_make_mock_session(age=10))
        pool._pool.put_nowait(_make_mock_session(age=10))
        full = _make_mock_session(age=10)
        assert await pool.release(full) == SessionReleaseOutcome(
            requeued=False,
            retired_reason="full",
        )

    @pytest.mark.asyncio
    async def test_first_mint_consumes_catalog_retirement_marker_once(self):
        pool, calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")

        await pool.record_catalog_retirement(session_id)
        async with pool._mint_usage_scope(1):
            pass
        async with pool._mint_usage_scope(1):
            pass

        assert calls[0]["cause_operation"] == "opportunistic_catalog_refresh"
        assert calls[0]["cause_session_id"] == session_id
        assert "cause_operation" not in calls[1]
        assert "cause_session_id" not in calls[1]

    @pytest.mark.asyncio
    async def test_tracker_exit_failure_restores_claim_for_next_mint(self):
        from app.bandwidth import ProxyUsageCapture

        pool, _calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        usages = []
        attempts = 0

        @asynccontextmanager
        async def track(**_kwargs):
            nonlocal attempts
            attempts += 1
            usage = ProxyUsageCapture()
            usages.append(usage)
            yield usage
            if attempts == 1:
                raise RuntimeError("ledger exit failed")
            usage.causal_event_persisted = usage.request_count > 0

        pool._proxy_usage.track.side_effect = track
        await pool.record_catalog_retirement(session_id)

        with pytest.raises(RuntimeError, match="ledger exit failed"):
            async with pool._mint_usage_scope(1) as usage:
                usage.request_count = 1
        async with pool._mint_usage_scope(1) as usage:
            usage.request_count = 1

        assert usages[1].cause_session_id == session_id

    @pytest.mark.asyncio
    async def test_finalize_failure_after_causal_insert_consumes_claim(self):
        from app.bandwidth import ProxyUsageCapture

        pool, _calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        usages = []
        attempts = 0

        @asynccontextmanager
        async def track(**_kwargs):
            nonlocal attempts
            attempts += 1
            usage = ProxyUsageCapture()
            usages.append(usage)
            yield usage
            if attempts == 1:
                usage.causal_event_persisted = True
                raise RuntimeError("finalize unavailable")

        pool._proxy_usage.track.side_effect = track
        await pool.record_catalog_retirement(session_id)

        with pytest.raises(RuntimeError, match="finalize unavailable"):
            async with pool._mint_usage_scope(1):
                pass
        async with pool._mint_usage_scope(1):
            pass

        assert usages[0].cause_session_id == session_id
        assert usages[1].cause_session_id is None

    @pytest.mark.asyncio
    async def test_provider_error_with_persisted_ledger_consumes_claim(self):
        from app.bandwidth import ProxyUsageCapture

        pool, _calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        usages = []

        @asynccontextmanager
        async def track(**_kwargs):
            usage = ProxyUsageCapture()
            usages.append(usage)
            try:
                yield usage
            except RuntimeError:
                usage.causal_event_persisted = usage.request_count > 0
                raise
            else:
                usage.causal_event_persisted = usage.request_count > 0

        pool._proxy_usage.track.side_effect = track
        await pool.record_catalog_retirement(session_id)

        with pytest.raises(RuntimeError, match="provider failed"):
            async with pool._mint_usage_scope(1) as usage:
                usage.request_count = 1
                raise RuntimeError("provider failed")
        async with pool._mint_usage_scope(1):
            pass

        assert usages[0].cause_session_id == session_id
        assert usages[0].causal_event_persisted is True
        assert usages[1].cause_session_id is None

    @pytest.mark.asyncio
    async def test_zero_provider_request_restores_claim_for_next_mint(self):
        from app.bandwidth import ProxyUsageCapture

        pool, _calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        usages = []

        @asynccontextmanager
        async def track(**_kwargs):
            usage = ProxyUsageCapture()
            usages.append(usage)
            yield usage
            usage.causal_event_persisted = usage.request_count > 0

        pool._proxy_usage.track.side_effect = track
        await pool.record_catalog_retirement(session_id)

        async with pool._mint_usage_scope(1):
            pass
        async with pool._mint_usage_scope(1) as usage:
            usage.request_count = 1

        assert usages[0].causal_event_persisted is False
        assert usages[1].cause_session_id == session_id

    @pytest.mark.asyncio
    async def test_tracker_enter_failure_restores_marker_for_next_mint(self):
        from app.bandwidth import ProxyUsageCapture

        pool, calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        attempts = 0

        @asynccontextmanager
        async def track(**kwargs):
            nonlocal attempts
            attempts += 1
            calls.append(kwargs)
            if attempts == 1:
                raise RuntimeError("budget reservation unavailable")
            usage = ProxyUsageCapture()
            yield usage
            kwargs["cause_session_id"] = usage.cause_session_id
            usage.causal_event_persisted = True

        pool._proxy_usage.track.side_effect = track
        await pool.record_catalog_retirement(session_id)

        with pytest.raises(RuntimeError, match="budget reservation unavailable"):
            async with pool._mint_usage_scope(1):
                pass
        async with pool._mint_usage_scope(1):
            pass

        assert "cause_session_id" not in calls[0]
        assert calls[1]["cause_session_id"] == session_id

    @pytest.mark.asyncio
    async def test_cancellation_during_tracker_enter_restores_marker(self):
        from app.bandwidth import ProxyUsageCapture

        pool, calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        enter_started = asyncio.Event()
        never_enter = asyncio.Event()

        @asynccontextmanager
        async def blocked_track(**kwargs):
            calls.append(kwargs)
            enter_started.set()
            await never_enter.wait()
            yield MagicMock(retry_count=0)

        pool._proxy_usage.track.side_effect = blocked_track
        await pool.record_catalog_retirement(session_id)

        async def mint_scope():
            async with pool._mint_usage_scope(1):
                pass

        cancelled = asyncio.create_task(mint_scope())
        await enter_started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        @asynccontextmanager
        async def successful_track(**kwargs):
            calls.append(kwargs)
            usage = ProxyUsageCapture()
            yield usage
            kwargs["cause_session_id"] = usage.cause_session_id
            usage.causal_event_persisted = True

        pool._proxy_usage.track.side_effect = successful_track
        await mint_scope()

        assert calls[1]["cause_session_id"] == session_id

    @pytest.mark.asyncio
    async def test_concurrent_mint_claims_without_waiting_for_first_tracker_enter(self):
        from app.bandwidth import ProxyUsageCapture

        pool, calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        first_entered = asyncio.Event()
        fail_first = asyncio.Event()

        @asynccontextmanager
        async def track(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                first_entered.set()
                await fail_first.wait()
                raise RuntimeError("reservation failed")
            usage = ProxyUsageCapture()
            yield usage
            kwargs["cause_session_id"] = usage.cause_session_id
            usage.causal_event_persisted = True

        pool._proxy_usage.track.side_effect = track
        await pool.record_catalog_retirement(session_id)

        async def mint_scope(*, fails=False):
            if fails:
                with pytest.raises(RuntimeError, match="reservation failed"):
                    async with pool._mint_usage_scope(1):
                        pass
                return
            async with pool._mint_usage_scope(1):
                pass

        first = asyncio.create_task(mint_scope(fails=True))
        await first_entered.wait()
        second = asyncio.create_task(mint_scope())
        await asyncio.sleep(0)
        assert len(calls) == 2
        assert calls[1]["cause_session_id"] == session_id
        fail_first.set()
        await first
        await second

        assert "cause_session_id" not in calls[0]

    @pytest.mark.asyncio
    async def test_concurrent_mint_never_waits_for_successful_owner(self):
        pool, calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        first_body = asyncio.Event()
        permit_first = asyncio.Event()
        await pool.record_catalog_retirement(session_id)

        async def first_mint():
            async with pool._mint_usage_scope(1):
                first_body.set()
                await permit_first.wait()

        async def second_mint():
            async with pool._mint_usage_scope(1):
                pass

        first = asyncio.create_task(first_mint())
        await first_body.wait()
        second = asyncio.create_task(second_mint())
        await asyncio.sleep(0)
        assert len(calls) == 2

        permit_first.set()
        await asyncio.gather(first, second)

        assert calls[0]["cause_session_id"] == session_id
        assert "cause_session_id" not in calls[1]

    @pytest.mark.asyncio
    async def test_claimed_marker_never_blocks_unattributed_concurrent_mint(self):
        pool, calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        await pool.record_catalog_retirement(session_id)
        assert pool._claim_catalog_retirement_marker() is not None

        async with pool._mint_usage_scope(1):
            pass

        assert "cause_session_id" not in calls[0]

    @pytest.mark.asyncio
    async def test_catalog_retirement_marker_expires_after_fixed_window(self, monkeypatch):
        pool, calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        clock = [100.0, 701.0]
        monkeypatch.setattr(
            "app.session_pool.time.monotonic",
            lambda: clock.pop(0) if clock else 701.0,
        )

        await pool.record_catalog_retirement(session_id)
        async with pool._mint_usage_scope(1):
            pass

        assert "cause_operation" not in calls[0]
        assert "cause_session_id" not in calls[0]

    @pytest.mark.asyncio
    async def test_unrelated_expired_release_does_not_mark_next_mint(self):
        pool, calls = self._proxy_pool_with_captured_usage()
        expired = _make_mock_session(age=pool._max_age)

        outcome = await pool.release(expired)
        async with pool._mint_usage_scope(1):
            pass

        assert outcome.retired_reason == "expired"
        assert "cause_operation" not in calls[0]
        assert "cause_session_id" not in calls[0]

    @pytest.mark.asyncio
    async def test_concurrent_mints_cannot_consume_same_marker_twice(self):
        pool, calls = self._proxy_pool_with_captured_usage()
        session_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        await pool.record_catalog_retirement(session_id)

        async def mint_scope():
            async with pool._mint_usage_scope(1):
                await asyncio.sleep(0)

        await asyncio.gather(mint_scope(), mint_scope())

        attributed = [
            call for call in calls
            if call.get("cause_operation") == "opportunistic_catalog_refresh"
        ]
        assert len(attributed) == 1
        assert attributed[0]["operation"] == "mint"
        assert attributed[0]["cause_session_id"] == session_id

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
