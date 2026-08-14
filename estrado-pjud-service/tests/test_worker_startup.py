import asyncio
from unittest.mock import AsyncMock, MagicMock
import httpx
import pytest

from worker.__main__ import (
    safe_initialize_pool,
    can_initialize_paid_pool,
    safe_get_next_batch,
    scheduler_contract_ready,
    wait_before_retry,
)
from datetime import datetime
from zoneinfo import ZoneInfo


def test_paid_pool_initialization_only_during_office_window():
    tz = ZoneInfo("America/Santiago")

    assert can_initialize_paid_pool(datetime(2026, 3, 2, 8, 0, tzinfo=tz)) is True
    assert can_initialize_paid_pool(datetime(2026, 3, 2, 18, 0, tzinfo=tz)) is False
    assert can_initialize_paid_pool(datetime(2026, 3, 1, 10, 0, tzinfo=tz)) is False


def test_one_shot_validation_can_initialize_outside_office_window():
    tz = ZoneInfo("America/Santiago")

    assert can_initialize_paid_pool(
        datetime(2026, 3, 2, 22, 0, tzinfo=tz), validation_once=True,
    ) is True


@pytest.mark.asyncio
async def test_one_shot_validation_never_waits_for_retry():
    shutdown = asyncio.Event()

    assert await wait_before_retry(shutdown, 30, validation_once=True) is False


@pytest.mark.asyncio
async def test_one_shot_main_exits_after_permanent_pool_init_failure(monkeypatch):
    from worker import __main__ as worker_main
    from worker.proxy_control import ProxyControlSnapshot

    config = MagicMock()
    config.PJUD_OFF_HOURS_VALIDATION_ONCE = True
    config.OJV_PROXY_URL = "http://proxy.invalid"
    config.OJV_PROXY_POOL_SIZE = 3
    config.POOL_SIZE = 3
    config.MINT_MAX_RETRIES = 3
    config.LOG_LEVEL = "INFO"
    config.TELEGRAM_BOT_TOKEN = ""
    config.TELEGRAM_CHAT_ID = ""
    config.SUPABASE_SERVICE_KEY = "secret-placeholder"
    config.OJV_PROXY_PRICE_PER_GB_USD = 1.0
    config.BLOCK_PAUSE_S = 0
    config.WORKER_ID = "test-worker"

    pool = MagicMock()
    pool.close_all = AsyncMock()
    metrics = MagicMock()
    metrics.stop = AsyncMock()
    backoff = MagicMock()
    backoff.is_permanently_open = True
    allowed = ProxyControlSnapshot(
        allowed=True, status="enabled", reason_code=None,
        revision=1, source="database",
    )

    monkeypatch.setattr(worker_main, "WorkerConfig", lambda: config)
    monkeypatch.setattr(worker_main, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(worker_main, "create_supabase", lambda _config: MagicMock())
    monkeypatch.setattr(worker_main, "ProxyUsageTracker", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(worker_main, "ProxyControl", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(worker_main, "SessionPool", lambda *_a, **_k: pool)
    monkeypatch.setattr(worker_main, "Scheduler", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(worker_main, "Notifier", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(worker_main, "Metrics", lambda *_a, **_k: metrics)
    monkeypatch.setattr(worker_main, "CircuitBreaker", lambda **_k: backoff)
    monkeypatch.setattr(worker_main.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(worker_main, "notify_ready", lambda: None)
    monkeypatch.setattr(worker_main, "notify_status", lambda *_a: None)
    monkeypatch.setattr(worker_main, "notify_stopping", lambda: None)
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", AsyncMock(return_value=allowed))
    monkeypatch.setattr(worker_main, "scheduler_contract_ready", AsyncMock(return_value=True))
    pool.initialize = AsyncMock(side_effect=RuntimeError("mint failed"))
    monkeypatch.setattr(worker_main, "send_ops_alert", AsyncMock())

    await asyncio.wait_for(worker_main.main(), timeout=0.2)

    pool.initialize.assert_awaited_once()
    assert config.OJV_PROXY_POOL_SIZE == 1
    assert config.POOL_SIZE == 1
    assert config.MINT_MAX_RETRIES == 1
    pool.close_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_failure_stays_alive_without_reinitializing_pool():
    scheduler = AsyncMock()
    scheduler.get_next_batch.side_effect = RuntimeError("RPC unavailable")
    metrics = MagicMock()
    backoff = MagicMock()

    result = await safe_get_next_batch(scheduler, metrics, backoff)

    assert result is None
    metrics.record_error.assert_called_once_with("infra")
    backoff.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_missing_claim_migration_blocks_before_paid_pool_init():
    scheduler = AsyncMock()
    scheduler.verify_claim_contract.side_effect = RuntimeError("RPC not found")
    metrics = MagicMock()
    backoff = MagicMock()

    assert await scheduler_contract_ready(scheduler, metrics, backoff) is False
    metrics.record_error.assert_called_once_with("infra")
    backoff.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_safe_initialize_retries_then_returns_false_no_crash(monkeypatch):
    pool = AsyncMock()
    pool.initialize = AsyncMock(side_effect=RuntimeError("mint failed"))
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("worker.__main__.asyncio.sleep", fake_sleep)
    ok = await safe_initialize_pool(pool, max_retries=3, base_delay=1)
    assert ok is False
    assert pool.initialize.await_count == 3
    assert len(slept) == 2  # backed off between attempts, not after the last one


@pytest.mark.asyncio
async def test_safe_initialize_succeeds_first_try(monkeypatch):
    pool = AsyncMock()
    pool.initialize = AsyncMock()  # succeeds
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("worker.__main__.asyncio.sleep", fake_sleep)
    ok = await safe_initialize_pool(pool, max_retries=3, base_delay=1)
    assert ok is True
    assert pool.initialize.await_count == 1
    assert slept == []


@pytest.mark.asyncio
async def test_safe_initialize_402_trips_control_and_never_retries(monkeypatch):
    pool = AsyncMock()
    pool.initialize.side_effect = httpx.ProxyError("402 Payment Required")
    control = AsyncMock()
    backoff = MagicMock()
    monkeypatch.setattr("worker.__main__.asyncio.sleep", AsyncMock())

    ok = await safe_initialize_pool(
        pool,
        max_retries=3,
        base_delay=1,
        proxy_control=control,
        backoff=backoff,
    )

    assert ok is False
    assert pool.initialize.await_count == 1
    control.trip_billing_exhausted.assert_awaited_once()
    backoff.open_permanently.assert_called_once_with("billing_exhausted")
