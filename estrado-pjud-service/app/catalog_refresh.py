"""Bounded, ready-session-only catalog refresh consumer."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from app.catalog_observations import (
    CatalogObservation,
    CatalogRefreshIntent,
    canonical_catalog_options,
    catalog_options_hash,
    is_partial_catalog,
)
from app.catalogs import CatalogContentError
from app.failure_kind import BlockedPageError
from app.proxy_billing import is_proxy_billing_error
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.session_pool import SessionReleaseOutcome

logger = logging.getLogger(__name__)


@dataclass
class CatalogRefreshMetrics:
    enqueued: int = 0
    queue_full: int = 0
    persistence_errors: int = 0
    session_release_errors: int = 0
    sessions_retired_by_catalog_refresh: int = 0


class CatalogRefreshQueue:
    def __init__(
        self,
        *,
        maxsize: int,
        pool,
        repository,
        catalog_service,
        proxy_usage,
        enabled: bool = False,
        breaker_retry_base_seconds: float = 0.25,
        breaker_retry_cap_seconds: float = 30.0,
        breaker_retry_sleep=asyncio.sleep,
        breaker_retry_random=random.random,
        breaker_retry_clock=time.monotonic,
        breaker_log_interval_seconds: float = 60.0,
    ) -> None:
        self._enabled = bool(enabled)
        self._queue: asyncio.Queue[CatalogRefreshIntent] = asyncio.Queue(
            maxsize=max(1, maxsize),
        )
        self._pool = pool
        self._repository = repository
        self._catalogs = catalog_service
        self._proxy_usage = proxy_usage
        self._consumer_task: asyncio.Task[None] | None = None
        self._telemetry_tasks: set[asyncio.Task[None]] = set()
        self._breaker_tasks: set[asyncio.Task[None]] = set()
        self._circuit_open = False
        self._stopping = False
        self._breaker_retry_base_seconds = max(0.01, breaker_retry_base_seconds)
        self._breaker_retry_cap_seconds = max(
            self._breaker_retry_base_seconds,
            breaker_retry_cap_seconds,
        )
        self._breaker_retry_sleep = breaker_retry_sleep
        self._breaker_retry_random = breaker_retry_random
        self._breaker_retry_clock = breaker_retry_clock
        self._breaker_log_interval_seconds = max(0.0, breaker_log_interval_seconds)
        self._last_breaker_failure_log_at: float | None = None
        self.metrics = CatalogRefreshMetrics()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open

    def enqueue(self, intent: CatalogRefreshIntent) -> bool:
        if not self._enabled or self._circuit_open:
            return False
        try:
            self._queue.put_nowait(intent)
        except asyncio.QueueFull:
            self.metrics.queue_full += 1
            self._schedule_event(intent, "queue_full")
            return False
        self.metrics.enqueued += 1
        self._schedule_event(intent, "enqueued")
        return True

    def enqueue_many(self, intents) -> int:
        accepted = 0
        seen: set[str] = set()
        for refresh_intent in intents:
            if refresh_intent.slice_key in seen:
                continue
            seen.add(refresh_intent.slice_key)
            if len(seen) > 2:
                break
            accepted += int(self.enqueue(refresh_intent))
        return accepted

    def _schedule_event(self, intent: CatalogRefreshIntent, outcome: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Construction/unit calls may happen before the app lifespan. There
            # is no safe background loop to attach to, and enqueue stays sync.
            return
        task = loop.create_task(self._record_event(intent, outcome))
        self._telemetry_tasks.add(task)
        task.add_done_callback(self._telemetry_tasks.discard)

    async def _record_event(self, intent: CatalogRefreshIntent, outcome: str) -> None:
        try:
            await self._repository.record_event(intent, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics.persistence_errors += 1
            logger.exception("Catalog refresh telemetry persistence failed")

    async def start(self) -> None:
        if not self._enabled or self._consumer_task is not None:
            return
        self._consumer_task = asyncio.create_task(self._consume_forever())

    async def stop(self, drain_timeout_seconds: float = 2) -> None:
        self._stopping = True
        timeout = max(0.0, min(2.0, drain_timeout_seconds))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if self._consumer_task is not None:
            consumer = self._consumer_task
            consumer.cancel()
            done, pending = await asyncio.wait({consumer}, timeout=timeout)
            if pending:
                logger.error("Catalog refresh consumer exceeded shutdown timeout")
            for task in done:
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
            self._consumer_task = consumer if pending else None
        tasks = tuple(self._telemetry_tasks | self._breaker_tasks)
        if not tasks:
            return
        remaining = max(0.0, deadline - loop.time())
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in pending:
            task.cancel()
        if pending:
            # Give cooperative cancellation one turn, but retain strong refs to
            # cancellation-resistant telemetry until its done callback fires.
            await asyncio.sleep(0)
            for task in tuple(self._telemetry_tasks):
                if task.done():
                    self._telemetry_tasks.discard(task)
                    try:
                        task.result()
                    except (asyncio.CancelledError, Exception):
                        pass
            for task in tuple(self._breaker_tasks):
                if task.done():
                    self._breaker_tasks.discard(task)
                    try:
                        task.result()
                    except (asyncio.CancelledError, Exception):
                        pass
        for task in done:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass

    async def _consume_forever(self) -> None:
        while not self._circuit_open:
            refresh_intent = await self._queue.get()
            try:
                await self.consume_one(refresh_intent)
            finally:
                self._queue.task_done()

    async def _persist(self, operation, *args) -> bool:
        try:
            await operation(*args)
            return True
        except Exception:
            self.metrics.persistence_errors += 1
            logger.exception("Catalog observation persistence failed")
            return False

    async def consume_one(self, intent: CatalogRefreshIntent) -> None:
        if not self._enabled or self._circuit_open:
            return
        try:
            control = await self._repository.control()
        except Exception:
            self.metrics.persistence_errors += 1
            logger.exception("Catalog refresh control persistence failed")
            return
        if control.circuit_open:
            self._circuit_open = True
            return
        if not control.opportunistic_enabled:
            return
        try:
            claim = await self._repository.claim(intent)
        except Exception:
            self.metrics.persistence_errors += 1
            logger.exception("Catalog refresh claim persistence failed")
            return
        if claim is None:
            return

        try:
            control_after_claim = await self._repository.control()
        except Exception:
            await self._persist(
                self._repository.fail,
                claim,
                "control_after_claim_error",
            )
            self.metrics.persistence_errors += 1
            logger.exception("Catalog refresh post-claim control persistence failed")
            return
        if control_after_claim.circuit_open:
            await self._persist(self._repository.fail, claim, "circuit_open")
            self._circuit_open = True
            return
        if not control_after_claim.opportunistic_enabled:
            await self._persist(self._repository.fail, claim, "disabled")
            return

        try:
            session = await self._pool.try_acquire_ready()
        except Exception:
            await self._persist(self._repository.fail, claim, "ready_pool_error")
            await self._persist(self._repository.record_event, intent, "error")
            return
        if session is None:
            await self._persist(self._repository.fail, claim, "no_ready_session")
            await self._persist(self._repository.record_event, intent, "no_ready_session")
            return

        healthy = True
        released = False
        breaker_opened_for_session = False
        try:
            params = intent.catalog_params()
            try:
                snapshot = canonical_catalog_options(
                    self._catalogs.snapshot_options(intent.catalog, params)
                )
            except Exception:
                await self._persist(self._repository.fail, claim, "snapshot_error")
                await self._persist(self._repository.record_event, intent, "error")
                return
            if not snapshot:
                await self._persist(
                    self._repository.fail,
                    claim,
                    "snapshot_unavailable",
                )
                await self._persist(self._repository.record_event, intent, "error")
                return
            try:
                # Budget reservation/capture surrounds only the actual PJUD POST.
                async with self._proxy_usage.track(
                    operation="opportunistic_catalog_refresh",
                    law_firm_id=intent.law_firm_id,
                    case_id=intent.case_id,
                    sync_run_id=intent.sync_run_id,
                ) as usage:
                    observed = await self._catalogs.fetch_with_session(
                        session,
                        intent.catalog,
                        params,
                        retry_transport=False,
                    )
            except (ProxyBudgetExceededError, ProxyUsagePersistenceError) as exc:
                await self._persist(self._repository.fail, claim, type(exc).__name__)
                await self._release_session(
                    session,
                    healthy=True,
                    claim=claim,
                    intent=intent,
                )
                released = True
                await self._persist(self._repository.record_event, intent, "error")
                return
            except (CatalogContentError, BlockedPageError) as exc:
                # Only blocked/invalid content is evidence that this session
                # generation is unsafe to reuse.
                healthy = False
                breaker_opened_for_session = True
                await self._open_retirement_circuit(
                    claim,
                    intent,
                    session,
                    f"{type(exc).__name__}: invalid catalog response",
                )
                await self._release_session(
                    session,
                    healthy=False,
                    claim=claim,
                    intent=intent,
                    breaker_already_open=breaker_opened_for_session,
                )
                released = True
                return
            except Exception as exc:
                # Timeouts and other transient transport failures are not, by
                # themselves, evidence that the session generation is corrupt.
                failure_reason = (
                    "provider_billing"
                    if is_proxy_billing_error(exc)
                    else type(exc).__name__
                )
                await self._persist(self._repository.fail, claim, failure_reason)
                await self._release_session(
                    session,
                    healthy=True,
                    claim=claim,
                    intent=intent,
                    breaker_already_open=breaker_opened_for_session,
                )
                released = True
                await self._persist(self._repository.record_event, intent, "error")
                return

            normalized = canonical_catalog_options(observed)
            observation = CatalogObservation(
                snapshot_hash=catalog_options_hash(snapshot),
                snapshot_options=snapshot,
                observed_hash=catalog_options_hash(normalized),
                options=normalized,
                session_generation_id=session.generation_id,
                bytes_up=max(0, getattr(usage, "bytes_up", 0)),
                bytes_down=max(0, getattr(usage, "bytes_down", 0)),
                partial=is_partial_catalog(snapshot, normalized),
            )
            completed = await self._persist(
                self._repository.complete,
                claim,
                observation,
            )
            await self._release_session(
                session,
                healthy=True,
                claim=claim,
                intent=intent,
            )
            released = True
            await self._persist(
                self._repository.record_event,
                intent,
                "success" if completed else "error",
            )
        finally:
            if not released:
                await self._release_session(
                    session,
                    healthy=healthy,
                    claim=claim,
                    intent=intent,
                    breaker_already_open=breaker_opened_for_session,
                )

    async def _release_session(
        self,
        session,
        *,
        healthy: bool,
        claim,
        intent,
        breaker_already_open: bool = False,
    ) -> SessionReleaseOutcome | None:
        try:
            outcome = await self._pool.release(session, healthy=healthy)
        except Exception as exc:
            self.metrics.session_release_errors += 1
            logger.exception("Catalog refresh session release failed")
            # The pool no longer owns this session. Fail closed locally and
            # make one best-effort close so a release bug cannot leak it alive.
            self._circuit_open = True
            try:
                await session.close()
            except Exception:
                logger.exception("Catalog refresh session close after release failed")
            if not breaker_already_open:
                await self._open_retirement_circuit(
                    claim,
                    intent,
                    session,
                    f"release_exception:{type(exc).__name__}",
                )
            return None

        if not isinstance(outcome, SessionReleaseOutcome):
            self.metrics.session_release_errors += 1
            logger.error("Catalog refresh pool returned no typed release outcome")
            if not breaker_already_open:
                await self._open_retirement_circuit(
                    claim,
                    intent,
                    session,
                    "release_outcome_missing",
                )
            return None

        if outcome.retired_reason is not None and not breaker_already_open:
            await self._open_retirement_circuit(
                claim,
                intent,
                session,
                f"release:{outcome.retired_reason}",
            )
        return outcome

    async def _open_retirement_circuit(
        self,
        claim,
        intent,
        session,
        reason: str,
    ) -> None:
        # Fail closed locally before any persistence, release, or optional event.
        self._circuit_open = True
        self.metrics.sessions_retired_by_catalog_refresh += 1
        try:
            await self._pool.record_catalog_retirement(session.generation_id)
        except asyncio.CancelledError:
            # Cancellation must not prevent the durable, cross-replica breaker.
            critical = self._track_breaker_task(self._persist_breaker_once(
                claim,
                intent,
                reason,
                session.generation_id,
            ))
            await asyncio.shield(critical)
            raise
        except Exception:
            # Causal attribution is best effort; safety remains authoritative.
            logger.exception("Catalog retirement causal marker failed")
        critical = self._track_breaker_task(self._persist_breaker_once(
            claim,
            intent,
            reason,
            session.generation_id,
        ))
        # Outer cancellation must not cancel the durable breaker write. The
        # strong task remains drainable by stop() for up to the shared 2s cap.
        await asyncio.shield(critical)

    def _track_breaker_task(self, coroutine) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._breaker_tasks.add(task)
        task.add_done_callback(self._breaker_tasks.discard)
        return task

    async def _persist_breaker_once(
        self,
        claim,
        intent,
        reason: str,
        session_generation_id,
    ) -> None:
        try:
            await self._repository.retire_and_open_circuit(
                claim,
                intent,
                reason,
                session_generation_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics.persistence_errors += 1
            self._log_breaker_failure(
                "Atomic catalog retirement breaker persistence failed"
            )
            self._schedule_breaker_retry(
                claim,
                intent,
                reason,
                session_generation_id,
            )

    def _schedule_breaker_retry(
        self,
        claim,
        intent,
        reason: str,
        session_generation_id,
    ) -> None:
        self._track_breaker_task(self._retry_breaker(
            claim,
            intent,
            reason,
            session_generation_id,
        ))

    def _log_breaker_failure(self, message: str) -> None:
        now = self._breaker_retry_clock()
        last = self._last_breaker_failure_log_at
        if last is None or now - last >= self._breaker_log_interval_seconds:
            # Static message only: never include reason, tenant IDs, or options.
            logger.exception(message)
            self._last_breaker_failure_log_at = now

    async def _retry_breaker(
        self,
        claim,
        intent,
        reason: str,
        session_generation_id,
    ) -> None:
        ceiling = self._breaker_retry_base_seconds
        while not self._stopping:
            try:
                await self._repository.retire_and_open_circuit(
                    claim,
                    intent,
                    reason,
                    session_generation_id,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                self.metrics.persistence_errors += 1
                self._log_breaker_failure("Catalog retirement breaker retry failed")
                random_value = min(1.0, max(0.0, self._breaker_retry_random()))
                jittered_delay = ceiling * (0.5 + 0.5 * random_value)
                await self._breaker_retry_sleep(jittered_delay)
                ceiling = min(
                    self._breaker_retry_cap_seconds,
                    ceiling * 2,
                )
