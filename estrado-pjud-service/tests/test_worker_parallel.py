import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.helpers import legacy_runtime_fence
from worker.__main__ import process_batch


class FakeBackoff:
    def __init__(self):
        self.is_open = False


class ConcurrencyTrackingEngine:
    """Fake engine that records concurrency overlap and which cases ran."""

    def __init__(self, delay_event: asyncio.Event | None = None):
        self.current = 0
        self.max_seen = 0
        self.ran = []
        self._delay_event = delay_event

    async def sync_case(self, case):
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        self.ran.append(case["id"])
        try:
            if self._delay_event is not None:
                await self._delay_event.wait()
            else:
                await asyncio.sleep(0)
        finally:
            self.current -= 1


class RaisingEngine:
    def __init__(self, bad_id):
        self.bad_id = bad_id
        self.ran = []

    async def sync_case(self, case):
        self.ran.append(case["id"])
        await asyncio.sleep(0)
        if case["id"] == self.bad_id:
            raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_process_batch_bounds_concurrency_to_n():
    delay_event = asyncio.Event()
    engine = ConcurrencyTrackingEngine(delay_event=delay_event)
    batch = [{"id": i} for i in range(6)]
    shutdown_event = asyncio.Event()
    backoff = FakeBackoff()

    task = asyncio.create_task(
        process_batch(batch, engine, 3, shutdown_event, backoff, runtime_fence=legacy_runtime_fence(), processing_window=lambda: True)
    )

    # Let the semaphore-bound tasks start and block on the delay event.
    for _ in range(20):
        await asyncio.sleep(0)
        if engine.current == 3:
            break

    assert engine.current == 3
    assert engine.max_seen == 3

    delay_event.set()
    await task

    assert engine.max_seen == 3
    assert sorted(engine.ran) == list(range(6))


@pytest.mark.asyncio
async def test_process_batch_skips_not_yet_started_on_shutdown():
    delay_event = asyncio.Event()
    engine = ConcurrencyTrackingEngine(delay_event=delay_event)
    batch = [{"id": i} for i in range(6)]
    shutdown_event = asyncio.Event()
    backoff = FakeBackoff()

    task = asyncio.create_task(
        process_batch(batch, engine, 2, shutdown_event, backoff, runtime_fence=legacy_runtime_fence(), processing_window=lambda: True)
    )

    # Wait until the first wave (bounded by N=2) has started.
    for _ in range(20):
        await asyncio.sleep(0)
        if engine.current == 2:
            break

    assert engine.current == 2
    # Trigger shutdown before releasing the delay; already-running cases should
    # still finish, but not-yet-started ones must be skipped.
    shutdown_event.set()
    delay_event.set()

    await task

    # Only the first wave of 2 should have run; the rest were skipped.
    assert len(engine.ran) == 2


@pytest.mark.asyncio
async def test_shutdown_drains_running_case_before_batch_release_and_skips_undispatched():
    running_started = asyncio.Event()
    allow_running_to_finish = asyncio.Event()
    running_finished = asyncio.Event()

    class DrainTrackingEngine:
        def __init__(self):
            self.ran = []

        async def sync_case(self, case):
            self.ran.append(case["id"])
            running_started.set()
            await allow_running_to_finish.wait()
            running_finished.set()

    engine = DrainTrackingEngine()
    shutdown_event = asyncio.Event()
    task = asyncio.create_task(
        process_batch(
            [{"id": 1}, {"id": 2}, {"id": 3}],
            engine,
            1,
            shutdown_event,
            FakeBackoff(),
            runtime_fence=legacy_runtime_fence(), processing_window=lambda: True,
        )
    )

    await asyncio.wait_for(running_started.wait(), timeout=0.1)
    shutdown_event.set()
    await asyncio.sleep(0)

    assert engine.ran == [1]
    assert running_finished.is_set() is False
    assert task.done() is False

    allow_running_to_finish.set()
    await asyncio.wait_for(task, timeout=0.1)

    assert running_finished.is_set() is True
    assert engine.ran == [1]


@pytest.mark.asyncio
async def test_process_batch_skips_when_circuit_breaker_opens_mid_batch():
    delay_event = asyncio.Event()
    engine = ConcurrencyTrackingEngine(delay_event=delay_event)
    batch = [{"id": i} for i in range(6)]
    shutdown_event = asyncio.Event()
    backoff = FakeBackoff()

    task = asyncio.create_task(
        process_batch(batch, engine, 2, shutdown_event, backoff, runtime_fence=legacy_runtime_fence(), processing_window=lambda: True)
    )

    for _ in range(20):
        await asyncio.sleep(0)
        if engine.current == 2:
            break

    assert engine.current == 2
    backoff.is_open = True
    delay_event.set()

    await task

    assert len(engine.ran) == 2


@pytest.mark.asyncio
async def test_process_batch_one_case_raising_does_not_sink_others():
    engine = RaisingEngine(bad_id=2)
    batch = [{"id": i} for i in range(5)]
    shutdown_event = asyncio.Event()
    backoff = FakeBackoff()

    # Should not raise, despite one case's sync_case raising.
    await process_batch(
        batch, engine, 5, shutdown_event, backoff, runtime_fence=legacy_runtime_fence(), processing_window=lambda: True,
    )

    assert sorted(engine.ran) == list(range(5))


@pytest.mark.asyncio
async def test_process_batch_rechecks_persistent_gate_before_each_case():
    from worker.proxy_control import ProxyControlSnapshot

    engine = ConcurrencyTrackingEngine()
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=False,
        status="paused",
        reason_code="ops_pause",
        revision=4,
        source="database",
    )
    backoff = MagicMock()
    backoff.is_open = False

    await process_batch(
        [{"id": 1}, {"id": 2}],
        engine,
        2,
        asyncio.Event(),
        backoff,
        proxy_control=control,
        runtime_fence=legacy_runtime_fence(), processing_window=lambda: True,
    )

    assert engine.ran == []
    assert control.refresh.await_count == 2


@pytest.mark.asyncio
async def test_process_batch_does_not_start_cases_after_office_window_closes():
    engine = ConcurrencyTrackingEngine()

    await process_batch(
        [{"id": 1}, {"id": 2}],
        engine,
        1,
        asyncio.Event(),
        FakeBackoff(),
        runtime_fence=legacy_runtime_fence(), processing_window=lambda: False,
    )

    assert engine.ran == []
