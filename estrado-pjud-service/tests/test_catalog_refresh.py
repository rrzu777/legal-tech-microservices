import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest

from app.catalog_observations import CatalogClaim, CatalogControl, CatalogRefreshIntent
from app.catalog_refresh import CatalogRefreshQueue
from app.catalogs import CatalogContentError
from app.catalogs import CatalogService
from app.config import Settings
from app.session import OJVSession
from app.adapters.http_adapter import OJVHttpAdapter
from app.proxy_cost import ProxyBudgetExceededError
from app.session_pool import SessionReleaseOutcome
from worker.proxy_usage import DISABLED_PROXY_USAGE


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
    pool.release.return_value = SessionReleaseOutcome(
        requeued=True,
        retired_reason=None,
    )
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

    async def fetch(*_args, **_kwargs):
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
    assert fakes.catalogs.fetch_with_session.await_args.kwargs == {
        "retry_transport": False,
    }


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

    events = []

    async def durable_breaker(*_args):
        assert fakes.queue.circuit_open is True
        events.append("breaker")

    async def release(*_args, **_kwargs):
        events.append("release")
        return SessionReleaseOutcome(requeued=False, retired_reason="unhealthy")

    async def telemetry(*_args):
        events.append("telemetry")

    fakes.repository.retire_and_open_circuit.side_effect = durable_breaker
    fakes.pool.release.side_effect = release
    fakes.repository.record_event.side_effect = telemetry

    await fakes.queue.consume_one(intent())

    fakes.pool.release.assert_awaited_once_with(session, healthy=False)
    fakes.repository.retire_and_open_circuit.assert_awaited_once()
    assert events[:2] == ["breaker", "release"]
    assert fakes.queue.circuit_open is True


@pytest.mark.asyncio
async def test_failed_durable_breaker_retries_in_background_without_more_pjud(fakes):
    session = MagicMock(generation_id=UUID("22222222-2222-4222-8222-222222222222"))
    fakes.pool.try_acquire_ready.return_value = session
    fakes.catalogs.fetch_with_session.side_effect = CatalogContentError("invalid")
    fakes.repository.retire_and_open_circuit.side_effect = [
        RuntimeError("database unavailable"),
        None,
    ]
    fakes.pool.release.return_value = SessionReleaseOutcome(
        requeued=False,
        retired_reason="unhealthy",
    )

    await fakes.queue.consume_one(intent())
    for _ in range(20):
        if fakes.repository.retire_and_open_circuit.await_count == 2:
            break
        await asyncio.sleep(0)

    assert fakes.queue.circuit_open is True
    assert fakes.repository.retire_and_open_circuit.await_count == 2
    assert fakes.catalogs.fetch_with_session.await_count == 1


@pytest.mark.asyncio
async def test_successful_refresh_opens_circuit_when_release_retires_session(fakes):
    session = MagicMock(generation_id=UUID("22222222-2222-4222-8222-222222222222"))
    fakes.pool.try_acquire_ready.return_value = session
    fakes.pool.release.return_value = SessionReleaseOutcome(
        requeued=False,
        retired_reason="expired",
    )

    await fakes.queue.consume_one(intent())

    assert fakes.queue.circuit_open is True
    fakes.repository.retire_and_open_circuit.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_exception_has_its_own_metric_and_fails_closed(fakes):
    session = MagicMock(generation_id=UUID("22222222-2222-4222-8222-222222222222"))
    fakes.pool.try_acquire_ready.return_value = session
    fakes.pool.release.side_effect = RuntimeError("close failed")

    await fakes.queue.consume_one(intent())

    assert fakes.queue.metrics.session_release_errors == 1
    assert fakes.queue.metrics.persistence_errors == 0
    assert fakes.queue.circuit_open is True
    fakes.repository.retire_and_open_circuit.assert_awaited_once()


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
async def test_two_intents_never_exceed_two_real_posts_on_transport_errors():
    adapter = OJVHttpAdapter(Settings(
        API_KEY="test",
        OJV_BASE_URL="https://x",
        RATE_LIMIT_MS=0,
        _env_file=None,
    ))
    adapter._client.post = AsyncMock(side_effect=httpx.ConnectError("proxy down"))
    session = OJVSession(adapter)
    pool = AsyncMock()
    pool.try_acquire_ready.return_value = session
    pool.release.return_value = SessionReleaseOutcome(
        requeued=True,
        retired_reason=None,
    )
    repository = AsyncMock()
    repository.control.return_value = CatalogControl(
        opportunistic_enabled=True,
        circuit_open=False,
    )
    repository.claim.side_effect = [
        claim("tribunals:civil:90"),
        claim("books:civil:90:2026"),
    ]
    catalogs = CatalogService(pool, snapshot={
        "generated_at": "2026-08-07T12:00:00+00:00",
        "tribunals": {
            "civil:90:1": {
                "fetched_at": "2026-08-07T12:00:00+00:00",
                "options": [{"code": "1", "label": "Uno"}],
            },
        },
        "books": {
            "civil:90:2026": {
                "fetched_at": "2026-08-07T12:00:00+00:00",
                "options": [{"code": "C", "label": "Civil"}],
            },
        },
    })
    queue = CatalogRefreshQueue(
        maxsize=2,
        enabled=True,
        pool=pool,
        repository=repository,
        catalog_service=catalogs,
        proxy_usage=DISABLED_PROXY_USAGE,
    )

    try:
        await queue.consume_one(intent("tribunals:civil:90"))
        await queue.consume_one(intent("books:civil:90:2026"))
    finally:
        await adapter.close()

    assert adapter._client.post.await_count == 2


@pytest.mark.asyncio
async def test_persistence_failures_never_escape_consumer(fakes):
    fakes.repository.claim.side_effect = RuntimeError("database unavailable")

    await fakes.queue.consume_one(intent())

    assert fakes.queue.metrics.persistence_errors == 1
    fakes.pool.try_acquire_ready.assert_not_awaited()


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
