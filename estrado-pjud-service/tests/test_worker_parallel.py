import asyncio

import pytest

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

    task = asyncio.create_task(process_batch(batch, engine, 3, shutdown_event, backoff))

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

    task = asyncio.create_task(process_batch(batch, engine, 2, shutdown_event, backoff))

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
async def test_process_batch_skips_when_circuit_breaker_opens_mid_batch():
    delay_event = asyncio.Event()
    engine = ConcurrencyTrackingEngine(delay_event=delay_event)
    batch = [{"id": i} for i in range(6)]
    shutdown_event = asyncio.Event()
    backoff = FakeBackoff()

    task = asyncio.create_task(process_batch(batch, engine, 2, shutdown_event, backoff))

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
    await process_batch(batch, engine, 5, shutdown_event, backoff)

    assert sorted(engine.ran) == list(range(5))
