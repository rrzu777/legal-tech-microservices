"""Independent local capacity/rate lanes for OJV work.

Database claims remain the distributed authority.  These lanes only bound one
process and deliberately do not share semaphores or rate clocks, so discovery,
authenticated private resolution and scheduled sync cannot starve each other.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager


class OjvLaneBudget:
    def __init__(self, concurrency: int, min_start_interval_seconds: float = 0.0):
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError("ojv_lane_concurrency_must_be_positive")
        if (
            isinstance(min_start_interval_seconds, bool)
            or not isinstance(min_start_interval_seconds, (int, float))
            or not 0 <= float(min_start_interval_seconds) <= 60
        ):
            raise ValueError("ojv_lane_rate_interval_out_of_range")
        self._semaphore = asyncio.Semaphore(concurrency)
        self._min_start_interval = float(min_start_interval_seconds)
        self._rate_lock = asyncio.Lock()
        self._next_start = 0.0
        self._accepting = True
        self._active_tasks: set[asyncio.Task] = set()

    def stop_accepting(self) -> None:
        self._accepting = False

    @asynccontextmanager
    async def slot(self):
        if not self._accepting:
            raise RuntimeError("ojv_work_budgets_stopping")
        task = asyncio.current_task()
        acquired = False
        if task is not None:
            self._active_tasks.add(task)
        try:
            await self._semaphore.acquire()
            acquired = True
            if not self._accepting:
                raise RuntimeError("ojv_work_budgets_stopping")
            async with self._rate_lock:
                delay = self._next_start - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next_start = time.monotonic() + self._min_start_interval
            if not self._accepting:
                raise RuntimeError("ojv_work_budgets_stopping")
            yield
        finally:
            if task is not None:
                self._active_tasks.discard(task)
            if acquired:
                self._semaphore.release()

    @property
    def active_tasks(self) -> set[asyncio.Task]:
        return set(self._active_tasks)

    async def drain(self, *, timeout_seconds: float) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds < 0
        ):
            raise ValueError("ojv_drain_timeout_invalid")
        current = asyncio.current_task()
        tasks = {
            task for task in self.active_tasks
            if task is not current and not task.done()
        }
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=float(timeout_seconds))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class OjvWorkBudgets:
    def __init__(
        self,
        *,
        discovery_concurrency: int,
        private_concurrency: int,
        scheduled_concurrency: int,
        discovery_min_start_interval_seconds: float = 0.0,
        private_min_start_interval_seconds: float = 0.0,
        scheduled_min_start_interval_seconds: float = 0.0,
    ):
        self.discovery = OjvLaneBudget(
            discovery_concurrency, discovery_min_start_interval_seconds,
        )
        self.private_resolution = OjvLaneBudget(
            private_concurrency, private_min_start_interval_seconds,
        )
        self.scheduled_sync = OjvLaneBudget(
            scheduled_concurrency, scheduled_min_start_interval_seconds,
        )
        self._lanes = (
            self.discovery, self.private_resolution, self.scheduled_sync,
        )

    def stop_accepting(self) -> None:
        for lane in self._lanes:
            lane.stop_accepting()

    async def drain(self, *, timeout_seconds: float) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds < 0:
            raise ValueError("ojv_drain_timeout_invalid")
        current = asyncio.current_task()
        tasks = {
            task
            for lane in self._lanes
            for task in lane.active_tasks
            if task is not current and not task.done()
        }
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=float(timeout_seconds))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
