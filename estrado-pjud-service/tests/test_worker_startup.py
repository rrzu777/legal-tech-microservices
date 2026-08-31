import asyncio
from unittest.mock import AsyncMock, MagicMock
import httpx
import pytest

from worker.__main__ import (
    safe_initialize_pool,
    can_initialize_paid_pool,
    safe_get_next_batch,
    safe_reconcile_stale_runs,
    scheduler_contract_ready,
    wait_before_retry,
)
from datetime import datetime
from zoneinfo import ZoneInfo


@pytest.fixture(autouse=True)
def _local_maintenance(monkeypatch, worker_maintenance):
    from worker import __main__ as worker_main
    from worker.maintenance_store import MaintenanceStore, ProcessIdentity
    monkeypatch.setattr(MaintenanceStore, "production", lambda: worker_maintenance.store)
    monkeypatch.setattr(ProcessIdentity, "current", lambda: worker_maintenance.identity)
    monkeypatch.setattr(worker_main, "WorkerMaintenance", lambda store, identity: worker_maintenance)


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_mode", [False, True])
async def test_startup_hold_keeps_real_heartbeat_watchdog_and_resumes(monkeypatch, worker_maintenance, proxy_mode):
    from worker import __main__ as worker_main
    from worker.metrics import Metrics
    from worker.maintenance import has_active_operation
    from worker.proxy_control import ProxyControl, ProxyControlSnapshot
    from tests.test_maintenance_heartbeat import guard_accepts
    from tests.test_maintenance_wiring import hold, assert_quiescent
    from dataclasses import replace

    config = _entrypoint_config()
    config.OJV_PROXY_URL = "http://proxy.invalid" if proxy_mode else ""
    config.ENABLE_PJUD_MY_CAUSES_IMPORT = True
    config.HEARTBEAT_INTERVAL_S = 0.001
    scheduler, pool = AsyncMock(), MagicMock(initialize=AsyncMock(), close_all=AsyncMock())
    heartbeat_db = MagicMock()
    control = ProxyControl(heartbeat_db) if proxy_mode else None
    metrics = Metrics(config, heartbeat_db, proxy_control=control, maintenance=worker_maintenance)
    _patch_entrypoint(monkeypatch, worker_main, config=config, scheduler=scheduler,
                      pool=pool, metrics=metrics, backoff=MagicMock(is_open=False))
    handlers = {}
    monkeypatch.setattr(worker_main.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    ready, heartbeat, reopen = asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(worker_main, "notify_ready", ready.set)
    watchdog_count = []
    def watchdog():
        assert not has_active_operation()
        watchdog_count.append(1)
        if len(watchdog_count) >= 2:
            heartbeat.set()
    monkeypatch.setattr("worker.metrics.notify_watchdog", watchdog)
    gate = AsyncMock(return_value=ProxyControlSnapshot(True, "enabled", None, 1, "database"))
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", gate)
    monkeypatch.setattr(worker_main, "can_initialize_paid_pool", lambda **kw: False)
    engine_factory = MagicMock()
    monkeypatch.setattr(worker_main, "SyncEngine", engine_factory)
    async def retry(*args, **kwargs):
        await reopen.wait()
        return True
    monkeypatch.setattr(worker_main, "wait_before_retry", retry)
    async def initialize(**kwargs):
        assert has_active_operation()
        handlers[worker_main.signal.SIGTERM](worker_main.signal.SIGTERM, None)
    pool.initialize.side_effect = initialize
    hold(worker_maintenance)
    running = asyncio.create_task(worker_main.main())
    try:
        await asyncio.wait_for(ready.wait(), 1)
        pool.initialize.assert_not_awaited()
        scheduler.reconcile_stale_runs.assert_not_awaited()
        await asyncio.wait_for(heartbeat.wait(), 1)
        assert metrics.initialization_started is False
        assert guard_accepts(metrics.heartbeat_payload(), worker_maintenance, proxy_mode=proxy_mode)
        if proxy_mode:
            assert control.snapshot.reason_code == "not_loaded"
        assert ready.is_set()
        assert_quiescent(worker_maintenance)
        pool.initialize.assert_not_awaited()
        scheduler.reconcile_stale_runs.assert_not_awaited()
        scheduler.verify_claim_contract.assert_not_awaited()
        scheduler.get_next_batch.assert_not_awaited()
        gate.assert_not_awaited()
        engine_factory.assert_not_called()
        control = worker_maintenance.store.read_control()
        worker_maintenance.store.transition(control.operation_id, "hold", replace(control, state="open"))
        reopen.set()
        await asyncio.wait_for(running, 1)
        assert metrics.initialization_started is True
        assert metrics.heartbeat_payload()["metadata"]["maintenance"] is None
        pool.initialize.assert_awaited_once_with(prewarm=False)
        scheduler.reconcile_stale_runs.assert_awaited_once()
        scheduler.verify_claim_contract.assert_awaited_once()
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def _entrypoint_config(*, validation_once=False):
    config = MagicMock()
    config.PJUD_OFF_HOURS_VALIDATION_ONCE = validation_once
    config.PJUD_PROCESS_OUTSIDE_OFFICE_HOURS = False
    config.OJV_PROXY_URL = "http://proxy.invalid"
    config.OJV_PROXY_POOL_SIZE = 3
    config.POOL_SIZE = 3
    config.MINT_MAX_RETRIES = 3
    config.LOG_LEVEL = "INFO"
    config.TELEGRAM_BOT_TOKEN = ""
    config.TELEGRAM_CHAT_ID = ""
    config.SUPABASE_SERVICE_KEY = "secret-placeholder"
    config.OJV_PROXY_PRICE_PER_GB_USD = 1.0
    config.OJV_PROXY_GB_BUDGET = 10.0
    config.OJV_PROXY_GB_ALERT_PCT = 80
    config.BLOCK_PAUSE_S = 0
    config.WORKER_ID = "test-worker"
    config.R2_ENABLED = False
    config.ENABLE_PJUD_MY_CAUSES_IMPORT = False
    config.R2_ACCESS_KEY_ID = ""
    return config


def _patch_entrypoint(monkeypatch, worker_main, *, config, scheduler, pool, metrics, backoff):
    monkeypatch.setattr(worker_main, "WorkerConfig", lambda: config)
    monkeypatch.setattr(worker_main, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(worker_main, "create_supabase", lambda _config: MagicMock())
    monkeypatch.setattr(worker_main, "ProxyUsageTracker", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(worker_main, "ProxyControl", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(worker_main, "SessionPool", lambda *_a, **_k: pool)
    monkeypatch.setattr(worker_main, "Scheduler", lambda *_a, **_k: scheduler)
    monkeypatch.setattr(worker_main, "Notifier", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(worker_main, "Metrics", lambda *_a, **_k: metrics)
    monkeypatch.setattr(worker_main, "CircuitBreaker", lambda **_k: backoff)
    monkeypatch.setattr(worker_main.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(worker_main, "notify_ready", lambda: None)
    monkeypatch.setattr(worker_main, "notify_status", lambda *_a: None)
    monkeypatch.setattr(worker_main, "notify_stopping", lambda: None)


def test_paid_pool_initialization_only_during_office_window():
    tz = ZoneInfo("America/Santiago")

    assert can_initialize_paid_pool(datetime(2026, 3, 2, 8, 0, tzinfo=tz)) is True
    assert can_initialize_paid_pool(datetime(2026, 3, 2, 18, 0, tzinfo=tz)) is False
    assert can_initialize_paid_pool(datetime(2026, 3, 1, 10, 0, tzinfo=tz)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("warm_start,reopen", [(False, False), (True, False), (False, True)])
async def test_manual_import_off_hours_and_scheduled_reopening(monkeypatch, warm_start, reopen):
    """Catch both the cold-start gate and the discovery-loop office-hours gate."""
    from worker import __main__ as worker_main
    from worker.proxy_control import ProxyControlSnapshot

    config = _entrypoint_config()
    config.ENABLE_PJUD_MY_CAUSES_IMPORT = True
    scheduler = AsyncMock()
    pool = MagicMock(initialize=AsyncMock(), close_all=AsyncMock())
    metrics = MagicMock(stop=AsyncMock())
    backoff = MagicMock(is_open=False)
    _patch_entrypoint(
        monkeypatch, worker_main, config=config, scheduler=scheduler,
        pool=pool, metrics=metrics, backoff=backoff,
    )
    handlers = {}
    monkeypatch.setattr(worker_main.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", AsyncMock(return_value=ProxyControlSnapshot(
        allowed=True, status="enabled", reason_code=None, revision=1, source="database",
    )))
    # Cold Sunday versus a pool that was started during office hours.
    monkeypatch.setattr(worker_main, "can_initialize_paid_pool", lambda **_kw: warm_start)
    monkeypatch.setattr(worker_main, "is_processing_allowed", lambda **_kw: reopen)
    engine = MagicMock(drain_work=AsyncMock())

    async def sync_case(_case):
        handlers[worker_main.signal.SIGTERM](worker_main.signal.SIGTERM, None)

    engine.sync_case = AsyncMock(side_effect=sync_case)
    scheduler.get_next_batch.return_value = [{"id": "scheduled-case"}]

    async def process_import():
        if not reopen:
            handlers[worker_main.signal.SIGTERM](worker_main.signal.SIGTERM, None)
        return False

    engine.process_import_job = AsyncMock(side_effect=process_import)
    monkeypatch.setattr(worker_main, "SyncEngine", lambda **_kw: engine)
    await asyncio.wait_for(worker_main.main(), timeout=0.3)

    if reopen:
        scheduler.get_next_batch.assert_awaited_once()
        engine.sync_case.assert_awaited_once_with({"id": "scheduled-case"})
    else:
        engine.process_import_job.assert_awaited_once()
        scheduler.get_next_batch.assert_not_awaited()
        engine.sync_case.assert_not_called()
    if warm_start:
        pool.initialize.assert_awaited_once_with()
    else:
        pool.initialize.assert_awaited_once_with(prewarm=False)
    pool.close_all.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["startup_reconcile", "proxy_refresh", "contract", "initialize",
                                      "recurrent_reconcile", "batch_claim", "batch_release"])
async def test_main_holds_complete_operation_at_each_effect_boundary(monkeypatch, worker_maintenance, boundary):
    from worker import __main__ as worker_main
    from worker.maintenance import has_active_operation
    from worker.proxy_control import ProxyControlSnapshot
    from tests.test_maintenance_wiring import hold, assert_held, assert_quiescent

    config = _entrypoint_config(validation_once=True)
    scheduler = AsyncMock()
    pool = MagicMock(initialize=AsyncMock(), close_all=AsyncMock())
    engine = MagicMock(sync_case=AsyncMock(), drain_work=AsyncMock())
    _patch_entrypoint(monkeypatch, worker_main, config=config, scheduler=scheduler,
                      pool=pool, metrics=MagicMock(stop=AsyncMock()), backoff=MagicMock(is_open=False))
    monkeypatch.setattr(worker_main, "SyncEngine", lambda **kw: engine)
    monkeypatch.setattr(worker_main, "can_initialize_paid_pool", lambda **kw: True)
    monkeypatch.setattr(worker_main, "is_processing_allowed", lambda **kw: True)
    monkeypatch.setattr(worker_main, "maybe_alert_bandwidth", AsyncMock())
    handlers = {}
    monkeypatch.setattr(worker_main.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    started, finish = asyncio.Event(), asyncio.Event()
    phases = []
    async def phase(name):
        assert has_active_operation(), f"unadmitted {name}"
        phases.append(name)
        if name == boundary:
            started.set()
            await finish.wait()
    async def reconcile():
        await phase("startup_reconcile" if not phases else "recurrent_reconcile")
    async def proxy(*args):
        await phase("proxy_refresh")
        return ProxyControlSnapshot(True, "enabled", None, 1, "database")
    async def contract():
        await phase("contract")
    async def initialize():
        await phase("initialize")
    async def claim():
        await phase("batch_claim")
        return [{"id": "case"}]
    async def release(ids):
        assert ids == ["case"]
        await phase("batch_release")
    async def sync(case):
        await phase("sync_case")
    scheduler.reconcile_stale_runs.side_effect = reconcile
    scheduler.verify_claim_contract.side_effect = contract
    scheduler.get_next_batch.side_effect = claim
    scheduler.release_batch.side_effect = release
    engine.sync_case.side_effect = sync
    pool.initialize.side_effect = initialize
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", proxy)
    async def retry(*a, **kw):
        handlers[worker_main.signal.SIGTERM](worker_main.signal.SIGTERM, None)
        return True
    monkeypatch.setattr(worker_main, "wait_before_retry", retry)
    running = asyncio.create_task(worker_main.main())
    try:
        await asyncio.wait_for(started.wait(), 1)
        hold(worker_maintenance)
        assert_held(worker_maintenance)
        finish.set()
        await asyncio.wait_for(running, 1)
        assert_quiescent(worker_maintenance)
        if boundary in {"batch_claim", "batch_release"}:
            assert phases[-1] == "batch_release"
            engine.sync_case.assert_awaited_once()
        else:
            scheduler.get_next_batch.assert_not_awaited()
    finally:
        finish.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def test_one_shot_validation_can_initialize_outside_office_window():
    tz = ZoneInfo("America/Santiago")

    assert can_initialize_paid_pool(
        datetime(2026, 3, 2, 22, 0, tzinfo=tz), validation_once=True,
    ) is True


def test_temporary_override_can_initialize_outside_office_window():
    tz = ZoneInfo("America/Santiago")

    assert can_initialize_paid_pool(
        datetime(2026, 3, 1, 22, 0, tzinfo=tz),
        process_outside_office_hours=True,
    ) is True


def test_paid_pool_cannot_initialize_during_maintenance_when_overrides_are_false():
    tz = ZoneInfo("America/Santiago")

    assert can_initialize_paid_pool(
        datetime(2026, 3, 2, 22, 0, tzinfo=tz),
        validation_once=False,
        process_outside_office_hours=False,
    ) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_by", ["disabled", "capacity", "proxy", "contract", "reconcile"])
async def test_manual_import_cold_start_retains_operational_gates(monkeypatch, blocked_by):
    from worker import __main__ as worker_main
    from worker.proxy_control import ProxyControlSnapshot

    config = _entrypoint_config()
    config.ENABLE_PJUD_MY_CAUSES_IMPORT = blocked_by != "disabled"
    config.OJV_PROXY_POOL_SIZE = 1 if blocked_by == "capacity" else 3
    scheduler = AsyncMock()
    if blocked_by == "contract":
        scheduler.verify_claim_contract.side_effect = RuntimeError("unavailable")
    if blocked_by == "reconcile":
        scheduler.reconcile_stale_runs.side_effect = RuntimeError("unavailable")
    pool = MagicMock(initialize=AsyncMock(), close_all=AsyncMock())
    engine_factory = MagicMock()
    _patch_entrypoint(
        monkeypatch, worker_main, config=config, scheduler=scheduler,
        pool=pool, metrics=MagicMock(stop=AsyncMock()), backoff=MagicMock(is_open=False),
    )
    monkeypatch.setattr(worker_main, "SyncEngine", engine_factory)
    monkeypatch.setattr(worker_main, "can_initialize_paid_pool", lambda **_kw: False)
    monkeypatch.setattr(worker_main, "wait_before_retry", AsyncMock(return_value=False))
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", AsyncMock(return_value=ProxyControlSnapshot(
        allowed=blocked_by != "proxy", status="paused" if blocked_by == "proxy" else "enabled",
        reason_code=None, revision=1, source="database",
    )))
    await worker_main.main()
    pool.initialize.assert_not_awaited()
    engine_factory.assert_not_called()
    scheduler.get_next_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_shot_validation_never_waits_for_retry():
    shutdown = asyncio.Event()

    assert await wait_before_retry(shutdown, 30, validation_once=True) is False


@pytest.mark.asyncio
async def test_one_shot_main_exits_after_permanent_pool_init_failure(monkeypatch):
    from worker import __main__ as worker_main
    from worker.proxy_control import ProxyControlSnapshot

    config = _entrypoint_config(validation_once=True)

    pool = MagicMock()
    pool.close_all = AsyncMock()
    metrics = MagicMock()
    metrics.stop = AsyncMock()
    backoff = MagicMock()
    backoff.is_permanently_open = True
    allowed = ProxyControlSnapshot(
        allowed=True, status="enabled", reason_code=None,
        revision=1, source="database",
    )

    scheduler = AsyncMock()
    _patch_entrypoint(
        monkeypatch, worker_main, config=config, scheduler=scheduler,
        pool=pool, metrics=metrics, backoff=backoff,
    )
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", AsyncMock(return_value=allowed))
    monkeypatch.setattr(worker_main, "scheduler_contract_ready", AsyncMock(return_value=True))
    pool.initialize = AsyncMock(side_effect=RuntimeError("mint failed"))
    monkeypatch.setattr(worker_main, "send_ops_alert", AsyncMock())

    await asyncio.wait_for(worker_main.main(), timeout=0.2)

    pool.initialize.assert_awaited_once()
    assert config.OJV_PROXY_POOL_SIZE == 1
    assert config.POOL_SIZE == 1
    assert config.MINT_MAX_RETRIES == 1
    pool.close_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_paused_entrypoint_reconciles_before_proxy_gate_without_mint(monkeypatch):
    """Persistent pause cannot suppress stale-run maintenance at startup."""
    from worker import __main__ as worker_main
    from worker.proxy_control import ProxyControlSnapshot

    config = _entrypoint_config(validation_once=True)
    scheduler = AsyncMock()
    pool = MagicMock(initialize=AsyncMock(), close_all=AsyncMock())
    metrics = MagicMock(stop=AsyncMock())
    backoff = MagicMock()
    denied = ProxyControlSnapshot(
        allowed=False, status="paused", reason_code="operator_pause",
        revision=2, source="database",
    )
    order = []

    async def reconcile():
        order.append("reconcile")

    async def gate_result(*_args):
        order.append("proxy_gate")
        return denied

    scheduler.reconcile_stale_runs.side_effect = reconcile
    _patch_entrypoint(
        monkeypatch, worker_main, config=config, scheduler=scheduler,
        pool=pool, metrics=metrics, backoff=backoff,
    )
    gate = AsyncMock(side_effect=gate_result)
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", gate)

    await asyncio.wait_for(worker_main.main(), timeout=0.2)

    scheduler.reconcile_stale_runs.assert_awaited_once_with()
    gate.assert_awaited_once()
    assert order == ["reconcile", "proxy_gate"]
    pool.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_outside_hours_main_loop_still_invokes_bounded_reconciliation(monkeypatch):
    """The live loop reaches maintenance before its office-hours early return."""
    from worker import __main__ as worker_main
    from worker.proxy_control import ProxyControlSnapshot

    config = _entrypoint_config()
    scheduler = AsyncMock()
    pool = MagicMock(initialize=AsyncMock(), close_all=AsyncMock())
    metrics = MagicMock(stop=AsyncMock())
    backoff = MagicMock(is_open=False)
    allowed = ProxyControlSnapshot(
        allowed=True, status="enabled", reason_code=None,
        revision=1, source="database",
    )
    _patch_entrypoint(
        monkeypatch, worker_main, config=config, scheduler=scheduler,
        pool=pool, metrics=metrics, backoff=backoff,
    )
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", AsyncMock(return_value=allowed))
    monkeypatch.setattr(worker_main, "can_initialize_paid_pool", MagicMock(return_value=True))
    monkeypatch.setattr(worker_main, "scheduler_contract_ready", AsyncMock(return_value=True))
    initialize_pool = AsyncMock(return_value=True)
    monkeypatch.setattr(worker_main, "safe_initialize_pool", initialize_pool)
    monkeypatch.setattr(worker_main, "is_processing_allowed", MagicMock(return_value=False))

    async def stop_after_office_gate(awaitable, *, timeout):
        awaitable.close()
        raise RuntimeError("stop after outside-hours loop")

    monkeypatch.setattr(worker_main.asyncio, "wait_for", stop_after_office_gate)

    with pytest.raises(RuntimeError, match="stop after outside-hours loop"):
        await worker_main.main()

    assert scheduler.reconcile_stale_runs.await_count == 2
    scheduler.get_next_batch.assert_not_awaited()
    initialize_pool.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciliation_failure_is_handled_and_blocks_startup_traffic(monkeypatch):
    """An unavailable maintenance RPC stays fail-closed before gate or pool."""
    from worker import __main__ as worker_main

    config = _entrypoint_config(validation_once=True)
    scheduler = AsyncMock()
    scheduler.reconcile_stale_runs.side_effect = RuntimeError("database unavailable")
    pool = MagicMock(initialize=AsyncMock(), close_all=AsyncMock())
    metrics = MagicMock(stop=AsyncMock())
    backoff = MagicMock()
    _patch_entrypoint(
        monkeypatch, worker_main, config=config, scheduler=scheduler,
        pool=pool, metrics=metrics, backoff=backoff,
    )
    gate = AsyncMock()
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", gate)

    await asyncio.wait_for(worker_main.main(), timeout=0.2)

    metrics.record_error.assert_called_once_with("infra")
    backoff.record_failure.assert_called_once()
    gate.assert_not_awaited()
    pool.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_reconciliation_returns_false_without_propagating():
    scheduler = AsyncMock()
    scheduler.reconcile_stale_runs.side_effect = RuntimeError("RPC unavailable")
    metrics = MagicMock()
    backoff = MagicMock()

    assert await safe_reconcile_stale_runs(scheduler, metrics, backoff) is False
    metrics.record_error.assert_called_once_with("infra")
    backoff.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_failure_stays_alive_without_reinitializing_pool():
    scheduler = AsyncMock()
    scheduler.get_next_batch.side_effect = RuntimeError("RPC unavailable")
    metrics = MagicMock()
    backoff = MagicMock()

    result = await safe_get_next_batch(scheduler, metrics, backoff)

    assert result is None
    metrics.record_error.assert_called_once_with("infra")
    backoff.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_missing_claim_migration_blocks_before_paid_pool_init():
    scheduler = AsyncMock()
    scheduler.verify_claim_contract.side_effect = RuntimeError("RPC not found")
    metrics = MagicMock()
    backoff = MagicMock()

    assert await scheduler_contract_ready(scheduler, metrics, backoff) is False
    metrics.record_error.assert_called_once_with("infra")
    backoff.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_safe_initialize_retries_then_returns_false_no_crash(monkeypatch):
    pool = AsyncMock()
    pool.initialize = AsyncMock(side_effect=RuntimeError("mint failed"))
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("worker.__main__.asyncio.sleep", fake_sleep)
    ok = await safe_initialize_pool(pool, max_retries=3, base_delay=1)
    assert ok is False
    assert pool.initialize.await_count == 3
    assert len(slept) == 2  # backed off between attempts, not after the last one


@pytest.mark.asyncio
async def test_safe_initialize_succeeds_first_try(monkeypatch):
    pool = AsyncMock()
    pool.initialize = AsyncMock()  # succeeds
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("worker.__main__.asyncio.sleep", fake_sleep)
    ok = await safe_initialize_pool(pool, max_retries=3, base_delay=1)
    assert ok is True
    assert pool.initialize.await_count == 1
    assert slept == []


@pytest.mark.asyncio
async def test_safe_initialize_402_trips_control_and_never_retries(monkeypatch):
    pool = AsyncMock()
    pool.initialize.side_effect = httpx.ProxyError("402 Payment Required")
    control = AsyncMock()
    backoff = MagicMock()
    monkeypatch.setattr("worker.__main__.asyncio.sleep", AsyncMock())

    ok = await safe_initialize_pool(
        pool,
        max_retries=3,
        base_delay=1,
        proxy_control=control,
        backoff=backoff,
    )

    assert ok is False
    assert pool.initialize.await_count == 1
    control.trip_billing_exhausted.assert_awaited_once()
    backoff.open_permanently.assert_called_once_with("billing_exhausted")
