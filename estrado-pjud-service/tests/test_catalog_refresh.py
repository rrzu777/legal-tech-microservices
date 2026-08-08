import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.catalog_observations import CatalogClaim, CatalogControl, CatalogRefreshIntent
from app.catalog_refresh import CatalogRefreshQueue
from app.catalogs import CatalogContentError
from app.proxy_cost import ProxyBudgetExceededError


def intent(slice_key: str = "tribunals:civil:90") -> CatalogRefreshIntent:
    parts = slice_key.split(":")
    return CatalogRefreshIntent(
        slice_key=slice_key,
        catalog=parts[0],
        competencia=parts[1],
        corte=int(parts[2]) if len(parts) > 2 and parts[2] else None,
        anno=int(parts[3]) if len(parts) > 3 and parts[3] else None,
        law_firm_id=None,
        case_id=None,
        sync_run_id=None,
        request_hash="a" * 64,
    )


def claim(slice_key: str = "tribunals:civil:90") -> CatalogClaim:
    return CatalogClaim(
        slice_key=slice_key,
        token=UUID("11111111-1111-4111-8111-111111111111"),
        lease_expires_at=None,
    )


@dataclass
class Fakes:
    queue: CatalogRefreshQueue
    pool: AsyncMock
    repository: AsyncMock
    catalogs: AsyncMock
    proxy_usage: MagicMock


@pytest.fixture
def fakes() -> Fakes:
    pool = AsyncMock()
    repository = AsyncMock()
    repository.control.return_value = CatalogControl(
        opportunistic_enabled=True,
        circuit_open=False,
    )
    repository.claim.return_value = claim()
    catalogs = MagicMock()
    catalogs.fetch_with_session = AsyncMock()
    catalogs.snapshot_options.return_value = [{"code": "1", "label": "Uno"}]
    catalogs.fetch_with_session.return_value = [{"code": "1", "label": "Uno"}]
    proxy_usage = MagicMock()

    @asynccontextmanager
    async def track(**_kwargs):
        yield SimpleNamespace(bytes_up=11, bytes_down=22)

    proxy_usage.track.side_effect = track
    return Fakes(
        queue=CatalogRefreshQueue(
            maxsize=2,
            enabled=True,
            pool=pool,
            repository=repository,
            catalog_service=catalogs,
            proxy_usage=proxy_usage,
        ),
        pool=pool,
        repository=repository,
        catalogs=catalogs,
        proxy_usage=proxy_usage,
    )


def test_queue_is_disabled_by_default():
    queue = CatalogRefreshQueue(
        maxsize=1,
        pool=AsyncMock(),
        repository=AsyncMock(),
        catalog_service=AsyncMock(),
        proxy_usage=AsyncMock(),
    )

    assert queue.enqueue(intent()) is False
    assert queue.pending == 0


def test_enqueue_is_bounded_and_nonblocking():
    queue = CatalogRefreshQueue(
        maxsize=1,
        enabled=True,
        pool=AsyncMock(),
        repository=AsyncMock(),
        catalog_service=AsyncMock(),
        proxy_usage=AsyncMock(),
    )
    assert queue.enqueue(intent()) is True
    assert queue.enqueue(intent("books:civil:90:2026")) is False
    assert queue.metrics.queue_full == 1


def test_enqueue_many_caps_each_user_result_at_two_unique_slices(fakes):
    accepted = fakes.queue.enqueue_many([
        intent(),
        intent(),
        intent("books:civil:90:2026"),
        intent("books:civil:90:2025"),
    ])

    assert accepted == 2
    assert fakes.queue.pending == 2


@pytest.mark.asyncio
async def test_consumer_skips_without_ready_session(fakes):
    fakes.pool.try_acquire_ready.return_value = None
    await fakes.queue.consume_one(intent())

    fakes.catalogs.fetch_with_session.assert_not_awaited()
    fakes.repository.fail.assert_awaited_once()
    assert fakes.repository.fail.await_args.args[1] == "no_ready_session"
    fakes.repository.record_event.assert_any_await(intent(), "no_ready_session")


@pytest.mark.asyncio
async def test_consumer_does_not_acquire_session_when_lease_is_denied(fakes):
    fakes.repository.claim.return_value = None
    await fakes.queue.consume_one(intent())

    fakes.pool.try_acquire_ready.assert_not_awaited()
    fakes.catalogs.fetch_with_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistent_open_circuit_stops_consumer_before_claim(fakes):
    fakes.repository.control.return_value = CatalogControl(
        opportunistic_enabled=True,
        circuit_open=True,
    )

    await fakes.queue.consume_one(intent())

    assert fakes.queue.circuit_open is True
    fakes.repository.claim.assert_not_awaited()
    fakes.pool.try_acquire_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_circuit_opened_after_claim_releases_lease_before_ready_acquire(fakes):
    fakes.repository.control.side_effect = [
        CatalogControl(opportunistic_enabled=True, circuit_open=False),
        CatalogControl(opportunistic_enabled=True, circuit_open=True),
    ]

    await fakes.queue.consume_one(intent())

    assert fakes.queue.circuit_open is True
    assert fakes.repository.fail.await_args.args[1] == "circuit_open"
    fakes.pool.try_acquire_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_scope_wraps_only_the_real_catalog_request(fakes):
    session = MagicMock(generation_id=UUID("22222222-2222-4222-8222-222222222222"))
    events = []
    fakes.pool.try_acquire_ready.side_effect = lambda: events.append("ready") or session
    fakes.repository.claim.side_effect = lambda _intent: events.append("claim") or claim()
    fakes.catalogs.snapshot_options.side_effect = lambda *_args: events.append("snapshot") or [
        {"code": "1", "label": "Uno"},
    ]

    @asynccontextmanager
    async def track(**kwargs):
        events.append(("track_enter", kwargs))
        yield SimpleNamespace(bytes_up=3, bytes_down=5)
        events.append("track_exit")

    async def fetch(*_args):
        events.append("request")
        return [{"code": "1", "label": "Uno"}]

    fakes.proxy_usage.track.side_effect = track
    fakes.catalogs.fetch_with_session.side_effect = fetch

    await fakes.queue.consume_one(intent())

    assert events == [
        "claim",
        "ready",
        "snapshot",
        ("track_enter", {
            "operation": "opportunistic_catalog_refresh",
            "law_firm_id": None,
            "case_id": None,
            "sync_run_id": None,
        }),
        "request",
        "track_exit",
    ]
    fakes.pool.release.assert_awaited_once_with(session, healthy=True)


@pytest.mark.asyncio
async def test_budget_denial_releases_healthy_and_does_not_call_pjud(fakes):
    session = MagicMock(generation_id=UUID("22222222-2222-4222-8222-222222222222"))
    fakes.pool.try_acquire_ready.return_value = session

    @asynccontextmanager
    async def denied(**_kwargs):
        raise ProxyBudgetExceededError("global")
        yield

    fakes.proxy_usage.track.side_effect = denied
    await fakes.queue.consume_one(intent())

    fakes.catalogs.fetch_with_session.assert_not_awaited()
    fakes.pool.release.assert_awaited_once_with(session, healthy=True)
    fakes.repository.fail.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_snapshot_skips_paid_request_and_releases_ready_session(fakes):
    session = MagicMock(generation_id=UUID("22222222-2222-4222-8222-222222222222"))
    fakes.pool.try_acquire_ready.return_value = session
    fakes.catalogs.snapshot_options.return_value = []

    await fakes.queue.consume_one(intent())

    fakes.proxy_usage.track.assert_not_called()
    fakes.catalogs.fetch_with_session.assert_not_awaited()
    fakes.pool.release.assert_awaited_once_with(session, healthy=True)
    assert fakes.repository.fail.await_args.args[1] == "snapshot_unavailable"


@pytest.mark.asyncio
async def test_partial_response_is_quarantined_for_repository_quorum(fakes):
    session = MagicMock(generation_id=UUID("22222222-2222-4222-8222-222222222222"))
    fakes.pool.try_acquire_ready.return_value = session
    fakes.catalogs.snapshot_options.return_value = [
        {"code": "1", "label": "Uno"},
        {"code": "2", "label": "Dos"},
    ]
    fakes.catalogs.fetch_with_session.return_value = [{"code": "1", "label": "Uno"}]

    await fakes.queue.consume_one(intent())

    observation = fakes.repository.complete.await_args.args[1]
    assert observation.partial is True
    assert observation.confirmed_by_full_refresh is False


@pytest.mark.asyncio
async def test_invalid_catalog_retires_session_opens_circuit_and_stops_consumer(fakes):
    session = MagicMock(generation_id=UUID("22222222-2222-4222-8222-222222222222"))
    fakes.pool.try_acquire_ready.return_value = session
    fakes.catalogs.fetch_with_session.side_effect = CatalogContentError("invalid")

    await fakes.queue.consume_one(intent())

    fakes.pool.release.assert_awaited_once_with(session, healthy=False)
    fakes.repository.open_circuit.assert_awaited_once()
    fakes.repository.record_event.assert_any_await(intent(), "session_retired")
    fakes.repository.record_event.assert_any_await(intent(), "circuit_opened")
    assert fakes.queue.circuit_open is True


@pytest.mark.asyncio
async def test_transient_network_failure_keeps_session_healthy_and_circuit_closed(fakes):
    session = MagicMock(generation_id=UUID("22222222-2222-4222-8222-222222222222"))
    fakes.pool.try_acquire_ready.return_value = session
    fakes.catalogs.fetch_with_session.side_effect = TimeoutError("temporary network")

    await fakes.queue.consume_one(intent())

    fakes.pool.release.assert_awaited_once_with(session, healthy=True)
    fakes.repository.open_circuit.assert_not_awaited()
    fakes.repository.record_event.assert_any_await(intent(), "error")
    assert fakes.queue.circuit_open is False


@pytest.mark.asyncio
async def test_persistence_failures_never_escape_consumer(fakes):
    fakes.repository.claim.side_effect = RuntimeError("database unavailable")

    await fakes.queue.consume_one(intent())

    assert fakes.queue.metrics.persistence_errors == 1


@pytest.mark.asyncio
async def test_stop_waits_for_telemetry_but_never_more_than_two_seconds():
    blocked = asyncio.Event()
    repository = AsyncMock()
    async def wait_for_shutdown(*_args):
        await blocked.wait()

    repository.record_event.side_effect = wait_for_shutdown
    queue = CatalogRefreshQueue(
        maxsize=1,
        enabled=True,
        pool=AsyncMock(),
        repository=repository,
        catalog_service=AsyncMock(),
        proxy_usage=AsyncMock(),
    )
    queue.enqueue(intent())

    started = asyncio.get_running_loop().time()
    await queue.stop(drain_timeout_seconds=0.02)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.2
    assert not queue._telemetry_tasks


@pytest.mark.asyncio
async def test_stop_timeout_also_bounds_a_stuck_consumer():
    entered = asyncio.Event()
    never = asyncio.Event()
    repository = AsyncMock()
    repository.control.return_value = CatalogControl(
        opportunistic_enabled=True,
        circuit_open=False,
    )

    async def stuck_claim(*_args):
        entered.set()
        await never.wait()

    repository.claim.side_effect = stuck_claim
    queue = CatalogRefreshQueue(
        maxsize=1,
        enabled=True,
        pool=AsyncMock(),
        repository=repository,
        catalog_service=MagicMock(),
        proxy_usage=MagicMock(),
    )
    await queue.start()
    queue.enqueue(intent())
    await entered.wait()

    started = asyncio.get_running_loop().time()
    await queue.stop(drain_timeout_seconds=0.02)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_stop_does_not_wait_forever_for_telemetry_suppressing_cancellation():
    entered = asyncio.Event()
    release = asyncio.Event()
    repository = AsyncMock()

    async def stubborn_event(*_args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    repository.record_event.side_effect = stubborn_event
    queue = CatalogRefreshQueue(
        maxsize=1,
        enabled=True,
        pool=AsyncMock(),
        repository=repository,
        catalog_service=MagicMock(),
        proxy_usage=MagicMock(),
    )
    queue.enqueue(intent())
    await entered.wait()

    started = asyncio.get_running_loop().time()
    await queue.stop(drain_timeout_seconds=0.02)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.2
    assert queue._telemetry_tasks
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not queue._telemetry_tasks
