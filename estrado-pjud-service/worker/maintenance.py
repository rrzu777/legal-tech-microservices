"""Cooperative admission for complete operations and their asynchronous auxiliaries.

Use one coordinator per process/event loop. Every side-effecting operation must
enter run() before creating work, and register thread/subprocess completion futures
before awaiting them. This does not infer remote success or coordinate legacy code.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

from .maintenance_store import Ack, AdmissionClosed, MaintenanceError, MaintenanceStore, ProcessIdentity


T = TypeVar("T")


@dataclass
class _Operation:
    owner: WorkerMaintenance
    auxiliaries: set[asyncio.Future] = field(default_factory=set)
    closed: bool = False


_active_operation: ContextVar[_Operation | None] = ContextVar("worker_maintenance_operation", default=None)
# Deliberately process-lifetime: even dropping an uncertain coordinator must not
# garbage-collect its safety lease and turn stale on-disk ACKs into valid proof.
_uncertainty_leases: list = []


def has_active_operation() -> bool:
    """Pre-creation boundary for helpers shared with unrelated API callers.

    Return False outside admission, True for a live operation, and raise for a
    closed inherited context BEFORE creating any future/thread/subprocess. Keep
    probe, future creation and track_auxiliary registration together with no await
    between them; otherwise the operation can finish before work is registered.
    """
    operation = _active_operation.get()
    if operation is None:
        return False
    if operation.closed:
        operation.owner.mark_uncertain()
        raise MaintenanceError()
    return True


def mark_uncertain() -> None:
    """Mark the current operation's process unsafe for proof; no automatic reset."""
    operation = _active_operation.get()
    if operation is None:
        raise MaintenanceError()
    operation.owner.mark_uncertain()


def track_auxiliary(future: asyncio.Future[T]) -> asyncio.Future[T]:
    """Register an un-cancelled completion future and return its shielded awaitable.

    Pass the original run_in_executor/create_task future BEFORE awaiting/cancelling
    it. Shared helpers must call has_active_operation BEFORE creating that future,
    without any await between probe, creation and registration. A cancelled wrapper
    cannot prove that its underlying thread stopped. Bare
    coroutines and foreign/concurrent.futures futures must be scheduled/wrapped by
    the caller first. This function never creates work on its own.
    """
    operation = _active_operation.get()
    if operation is None or operation.closed:
        if operation is not None:
            operation.owner.mark_uncertain()
        raise MaintenanceError()
    if not isinstance(future, asyncio.Future) or future.get_loop() is not asyncio.get_running_loop():
        operation.owner.mark_uncertain()
        raise MaintenanceError()
    if future.cancelled():
        operation.owner.mark_uncertain()
        raise MaintenanceError()
    operation.auxiliaries.add(future)
    shielded = asyncio.shield(future)
    # If caller registers but does not await, retrieve wrapper errors too. The
    # original remains tracked and its outcome is checked during drain below.
    shielded.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return shielded


class WorkerMaintenance:
    def __init__(self, store: MaintenanceStore, identity: ProcessIdentity) -> None:
        if type(identity) is not ProcessIdentity:
            raise MaintenanceError()
        self.store = store
        self.identity = identity
        self._inflight = 0
        self._uncertain = False
        self._safety_lease = None

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def uncertain(self) -> bool:
        return self._uncertain

    def mark_uncertain(self) -> None:
        self._uncertain = True
        if self._safety_lease is None and self._inflight == 0:
            lease = self.store.shared_lease()
            try:
                lease.__enter__()
            except MaintenanceError:
                # If an operator already owns EX, no new work is admitted. A
                # later call retries SH, but never manufactures a safe ACK.
                return
            self._retain_uncertain_lease(lease)

    def _retain_uncertain_lease(self, lease) -> None:
        self._safety_lease = lease
        _uncertainty_leases.append(lease)

    def publish_ack(self) -> Ack:
        """Never quiescent for open, live operations, invalid control or uncertainty."""
        try:
            control = self.store.read_control()
            state = "quiescent" if control.state == "hold" and self._inflight == 0 and not self._uncertain else "draining"
            ack = Ack(1, control.operation_id, self.identity.boot_id, self.identity.pid,
                      self.identity.start_ticks, self.identity.instance_id, state, self._inflight)
            self.store.write_ack(ack)
            return ack
        except MaintenanceError:
            self.mark_uncertain()
            raise

    def _publish_best_effort(self) -> None:
        try:
            self.publish_ack()
        except MaintenanceError:
            # Existing errors retain their type; never substitute a success ACK.
            self.mark_uncertain()

    async def _finish_auxiliaries(self, operation: _Operation) -> bool:
        cancelled = False
        # Children inherit the operation context and can register further work
        # before their own completion. Iterate until every registered future ends.
        observed: set[asyncio.Future] = set()
        while pending := operation.auxiliaries - observed:
            for future in pending:
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        self.mark_uncertain()
                        cancelled = True
                    except BaseException:
                        self.mark_uncertain()
                if future.cancelled():
                    self.mark_uncertain()
                elif future.exception() is not None:
                    self.mark_uncertain()
                observed.add(future)
        return cancelled

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Admit before calling operation; retain its lease through all tracked work.

        Hold never cancels work. External cancellation is sticky uncertain and is
        re-raised only after tracked completion futures settle, even if cancellation
        repeats. Errors are propagated without retries. Unknown outcomes must be
        explicitly marked if application code catches an exception internally.
        """
        if _active_operation.get() is not None:
            # Nested admission can deadlock behind a new operator hold. Private
            # work must stay inside the existing operation instead of reacquiring.
            raise MaintenanceError()
        lease = self.store.shared_lease()
        leased = False
        try:
            if self._uncertain:
                raise AdmissionClosed()
            lease.__enter__()
            leased = True
            control = self.store.read_control()
            if control.state != "open":
                raise AdmissionClosed()
            self._inflight += 1  # No await between control check and counting.
            active = _Operation(self)
            token = _active_operation.set(active)
            cancelled_during_cleanup = False
            try:
                self.publish_ack()  # Capability must be writable before work.
                return await operation()
            except BaseException:
                self.mark_uncertain()
                self._publish_best_effort()
                raise
            finally:
                try:
                    cancelled_during_cleanup = await self._finish_auxiliaries(active)
                finally:
                    active.closed = True
                    _active_operation.reset(token)
                    self._inflight -= 1
                    if self._uncertain and self._safety_lease is None:
                        self._retain_uncertain_lease(lease)
                    self._publish_best_effort()
                if cancelled_during_cleanup:
                    raise asyncio.CancelledError()
        except AdmissionClosed:
            self._publish_best_effort()
            raise
        except MaintenanceError:
            self.mark_uncertain()
            self._publish_best_effort()
            raise
        finally:
            if leased:
                if self._uncertain and self._safety_lease is None:
                    self._retain_uncertain_lease(lease)
                if lease is not self._safety_lease:
                    lease.__exit__(None, None, None)
