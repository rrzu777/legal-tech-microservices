from unittest.mock import AsyncMock, MagicMock
import httpx
import pytest

from worker.__main__ import (
    safe_initialize_pool,
    can_initialize_paid_pool,
    safe_get_next_batch,
    scheduler_contract_ready,
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
