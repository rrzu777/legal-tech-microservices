"""Admission owns the driver until its real exit settles, not just its waiter."""
import asyncio

from worker.maintenance import has_active_operation, mark_uncertain, track_auxiliary


def owned_playwright(factory, *, cleanup_timeout: float):
    # API callers retain the original context manager and exception behavior.
    admitted = has_active_operation()
    manager = factory()
    return _OwnedRuntime(manager, cleanup_timeout) if admitted else manager


class _OwnedRuntime:
    def __init__(self, manager, timeout):
        self._manager = manager
        self._timeout = timeout
        self._exit_task = None
        self._enter_started = False

    async def __aenter__(self):
        self._enter_started = True
        try:
            return await self._manager.__aenter__()
        except BaseException:
            # Enter may already have spawned Connection.run()/its driver.
            # Preserve the original error even when its cleanup also fails.
            try:
                await self.__aexit__(None, None, None)
            except BaseException:
                pass
            raise

    async def __aexit__(self, *args):
        has_active_operation()
        if not self._enter_started:
            # A local configuration failure before enter cannot have started
            # a driver. Do not invent runtime uncertainty from an invalid exit.
            return None
        if self._exit_task is None:
            self._exit_task = asyncio.create_task(self._manager.__aexit__(*args))
        try:
            # The original completion task is never cancelled by a timeout or
            # repeated cancellation of either caller. The coordinator joins it
            # under the original SH lease; a failed exit retains safety SH.
            return await asyncio.wait_for(track_auxiliary(self._exit_task), self._timeout)
        except BaseException:
            # Mark before official login / pool initialization absorbs errors.
            mark_uncertain()
            raise
