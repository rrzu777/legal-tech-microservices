import asyncio
import time
from contextlib import suppress

import pytest
from unittest.mock import AsyncMock

from app.ojv.budget import OjvLaneBudget, OjvWorkBudgets


@pytest.mark.asyncio
async def test_saturated_discovery_lane_cannot_starve_private_or_scheduled_work():
    budgets = OjvWorkBudgets(
        discovery_concurrency=1,
        private_concurrency=1,
        scheduled_concurrency=1,
    )
    discovery_entered = asyncio.Event()
    release_discovery = asyncio.Event()

    async def discovery():
        async with budgets.discovery.slot():
            discovery_entered.set()
            await release_discovery.wait()

    first = asyncio.create_task(discovery())
    await discovery_entered.wait()
    second = asyncio.create_task(discovery())

    private_done = asyncio.Event()
    scheduled_done = asyncio.Event()
    async with budgets.private_resolution.slot():
        private_done.set()
    async with budgets.scheduled_sync.slot():
        scheduled_done.set()

    assert private_done.is_set()
    assert scheduled_done.is_set()
    assert not second.done()
    release_discovery.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_each_lane_enforces_its_own_concurrency_and_start_rate():
    budgets = OjvWorkBudgets(
        discovery_concurrency=1,
        private_concurrency=2,
        scheduled_concurrency=1,
        private_min_start_interval_seconds=0.02,
    )
    active = 0
    peak = 0
    starts: list[float] = []

    async def private_work():
        nonlocal active, peak
        async with budgets.private_resolution.slot():
            starts.append(time.monotonic())
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1

    await asyncio.gather(private_work(), private_work(), private_work())

    assert peak == 2
    assert starts[1] - starts[0] >= 0.018
    assert starts[2] - starts[1] >= 0.018


@pytest.mark.asyncio
async def test_structured_shutdown_stops_claiming_and_cancels_bounded_inflight_work():
    budgets = OjvWorkBudgets(
        discovery_concurrency=1,
        private_concurrency=1,
        scheduled_concurrency=1,
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_finishes():
        try:
            async with budgets.private_resolution.slot():
                entered.set()
                await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(never_finishes())
    await entered.wait()
    queued = asyncio.create_task(never_finishes())
    await asyncio.sleep(0)
    budgets.stop_accepting()

    with pytest.raises(RuntimeError, match="ojv_work_budgets_stopping"):
        async with budgets.discovery.slot():
            pass

    await budgets.drain(timeout_seconds=0.01)
    with suppress(asyncio.CancelledError):
        await task
    with suppress(asyncio.CancelledError):
        await queued
    assert cancelled.is_set()
    assert task.cancelled()
    assert queued.cancelled()


@pytest.mark.asyncio
async def test_single_api_lane_has_the_same_bounded_drain_contract():
    lane = OjvLaneBudget(1)
    entered = asyncio.Event()

    async def never_finishes():
        async with lane.slot():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(never_finishes())
    await entered.wait()
    lane.stop_accepting()
    await lane.drain(timeout_seconds=0.01)

    assert task.cancelled()


def test_invalid_lane_budget_is_rejected_before_worker_start():
    with pytest.raises(ValueError, match="ojv_lane_concurrency_must_be_positive"):
        OjvWorkBudgets(
            discovery_concurrency=0,
            private_concurrency=1,
            scheduled_concurrency=1,
        )
    with pytest.raises(ValueError, match="ojv_lane_rate_interval_out_of_range"):
        OjvWorkBudgets(
            discovery_concurrency=1,
            private_concurrency=1,
            scheduled_concurrency=1,
            private_min_start_interval_seconds=-1,
        )


@pytest.mark.asyncio
async def test_scheduled_shutdown_bounds_and_cancels_inflight_work_without_acknowledging():
    from tests.helpers import legacy_runtime_fence
    from worker.__main__ import process_batch

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Engine:
        async def sync_case(self, _case):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    shutdown = asyncio.Event()
    backoff = type("Backoff", (), {"is_open": False})()
    task = asyncio.create_task(process_batch(
        [{"id": "case-1"}], Engine(), 1, shutdown, backoff,
        runtime_fence=legacy_runtime_fence(), processing_window=lambda: True,
        shutdown_grace_seconds=0.01,
    ))
    await entered.wait()
    shutdown.set()
    await asyncio.wait_for(task, timeout=0.2)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_engine_dispatches_scheduled_and_private_work_through_distinct_lanes():
    from worker.engine import SyncEngine

    budgets = OjvWorkBudgets(
        discovery_concurrency=1,
        private_concurrency=1,
        scheduled_concurrency=1,
    )
    engine = SyncEngine.__new__(SyncEngine)
    engine._work_budgets = budgets
    engine._sync_case_unbudgeted = AsyncMock(return_value={"success": True})

    private_entered = asyncio.Event()
    release_private = asyncio.Event()

    async def private_operation():
        private_entered.set()
        await release_private.wait()
        return "resolved"

    private_task = asyncio.create_task(
        engine.run_private_resolution(private_operation),
    )
    await private_entered.wait()

    assert await engine.sync_case({"id": "case-1"}) == {"success": True}
    engine._sync_case_unbudgeted.assert_awaited_once_with({"id": "case-1"})
    release_private.set()
    assert await private_task == "resolved"
