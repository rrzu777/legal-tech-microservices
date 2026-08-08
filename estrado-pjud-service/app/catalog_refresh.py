"""Bounded, ready-session-only catalog refresh consumer."""

from __future__ import annotations

import asyncio
import logging
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
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError

logger = logging.getLogger(__name__)


@dataclass
class CatalogRefreshMetrics:
    enqueued: int = 0
    queue_full: int = 0
    persistence_errors: int = 0
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
        self._circuit_open = False
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
        tasks = tuple(self._telemetry_tasks)
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
                    )
            except (ProxyBudgetExceededError, ProxyUsagePersistenceError) as exc:
                await self._persist(self._repository.fail, claim, type(exc).__name__)
                await self._persist(self._repository.record_event, intent, "error")
                return
            except (CatalogContentError, BlockedPageError) as exc:
                # Only blocked/invalid content is evidence that this session
                # generation is unsafe to reuse.
                healthy = False
                await self._persist(self._repository.fail, claim, type(exc).__name__)
                await self._release_session(session, healthy=False)
                released = True
                await self._retire_and_open_circuit(intent, session, exc)
                return
            except Exception as exc:
                # Timeouts and other transient transport failures are not, by
                # themselves, evidence that the session generation is corrupt.
                await self._persist(self._repository.fail, claim, type(exc).__name__)
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
            await self._persist(
                self._repository.record_event,
                intent,
                "success" if completed else "error",
            )
        finally:
            if not released:
                await self._release_session(session, healthy=healthy)

    async def _release_session(self, session, *, healthy: bool) -> None:
        try:
            await self._pool.release(session, healthy=healthy)
        except Exception:
            self.metrics.persistence_errors += 1
            logger.exception("Catalog refresh session release failed")

    async def _retire_and_open_circuit(self, intent, session, exc: Exception) -> None:
        self.metrics.sessions_retired_by_catalog_refresh += 1
        await self._persist(self._repository.record_event, intent, "session_retired")
        # Stop locally even if the durable RPC is unavailable. When it succeeds,
        # the one-way DB circuit stops all replicas on their next control load.
        self._circuit_open = True
        await self._persist(
            self._repository.open_circuit,
            f"{type(exc).__name__}: catalog session {session.generation_id} retired",
        )
        await self._persist(self._repository.record_event, intent, "circuit_opened")
