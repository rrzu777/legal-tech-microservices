import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_maintenance_heartbeat import guard_accepts
from tests.test_maintenance_wiring import assert_quiescent, hold
from tests.test_worker_startup import _entrypoint_config


class HeartbeatOnlySupabase:
    """Record the only database effect allowed to the rollback worker."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.violations: list[str] = []
        self._pending: dict | None = None

    def from_(self, table: str):
        if table != "sync_worker_heartbeats":
            self.violations.append(f"table:{table}")
        return self

    def upsert(self, payload: dict, *, on_conflict: str):
        if on_conflict != "worker_id":
            self.violations.append(f"conflict:{on_conflict}")
        self._pending = deepcopy(payload)
        return self

    def rpc(self, name: str, _payload: dict):
        self.violations.append(f"rpc:{name}")
        return self

    def execute(self):
        if self._pending is None:
            self.violations.append("execute_without_heartbeat")
            return SimpleNamespace(data=[])
        self.rows.append(self._pending)
        self._pending = None
        return SimpleNamespace(data=[])


@pytest.mark.asyncio
async def test_rollback_build_stays_observable_and_never_enters_pjud_work(
    monkeypatch, worker_maintenance,
):
    """Catch any mutable mode or normal-worker path escaping the rollback hold."""
    from worker import __main__ as worker_main
    from worker.metrics import Metrics

    config = _entrypoint_config(validation_once=True)
    config.PJUD_IMPORT_TRIAL_ONCE = True
    config.ENABLE_PJUD_MY_CAUSES_IMPORT = True
    config.HEARTBEAT_INTERVAL_S = 0.001
    database = HeartbeatOnlySupabase()
    handlers: dict[int, object] = {}
    ready = asyncio.Event()
    heartbeat = asyncio.Event()
    statuses: list[str] = []
    config_inputs: list[dict] = []

    hold(worker_maintenance)
    monkeypatch.setattr(
        worker_main,
        "WorkerConfig",
        lambda **kwargs: config_inputs.append(kwargs) or config,
    )
    monkeypatch.setattr(worker_main, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(worker_main.MaintenanceStore, "production", lambda: worker_maintenance.store)
    monkeypatch.setattr(worker_main.ProcessIdentity, "current", lambda: worker_maintenance.identity)
    monkeypatch.setattr(worker_main, "WorkerMaintenance", lambda _store, _identity: worker_maintenance)
    monkeypatch.setattr(worker_main, "create_supabase", lambda _config: database)
    monkeypatch.setattr(worker_main.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(worker_main, "notify_ready", ready.set)
    monkeypatch.setattr(worker_main, "notify_status", statuses.append)
    monkeypatch.setattr(worker_main, "notify_stopping", lambda: statuses.append("stopping"))
    monkeypatch.setattr("worker.metrics.notify_watchdog", heartbeat.set)

    validate_trial = MagicMock(side_effect=AssertionError("mutable trial mode escaped rollback hold"))
    monkeypatch.setattr(worker_main, "validate_import_trial_mode", validate_trial)
    forbidden_factories = {
        name: MagicMock(side_effect=AssertionError(f"rollback hold constructed {name}"))
        for name in (
            "create_trial_supabase",
            "ProxyUsageTracker",
            "SessionPool",
            "Scheduler",
            "Notifier",
            "RuntimeFence",
            "SyncEngine",
        )
    }
    for name, factory in forbidden_factories.items():
        monkeypatch.setattr(worker_main, name, factory)

    real_metrics = Metrics
    captured_metrics: list[Metrics] = []

    def metrics_factory(*args, **kwargs):
        metrics = real_metrics(*args, **kwargs)
        captured_metrics.append(metrics)
        return metrics

    monkeypatch.setattr(worker_main, "Metrics", metrics_factory)

    running = asyncio.create_task(worker_main.main())
    try:
        ready_wait = asyncio.create_task(ready.wait())
        done, _ = await asyncio.wait(
            {running, ready_wait}, timeout=1, return_when=asyncio.FIRST_COMPLETED,
        )
        assert ready_wait in done and running not in done, (
            "rollback-only worker must become ready and remain alive"
        )
        await asyncio.wait_for(heartbeat.wait(), 1)

        assert len(captured_metrics) == 1
        metrics = captured_metrics[0]
        assert metrics.current_status == "paused"
        assert config_inputs == [{
            "PJUD_OFF_HOURS_VALIDATION_ONCE": False,
            "PJUD_IMPORT_TRIAL_ONCE": False,
            "PJUD_IMPORT_TRIAL_CAPABILITY": None,
            "PJUD_PROCESS_OUTSIDE_OFFICE_HOURS": False,
            "ENABLE_PJUD_MY_CAUSES_IMPORT": False,
            "ENABLE_PJUD_MY_CAUSES_EXCEL": False,
        }]
        assert database.rows
        assert database.rows[-1]["metadata"]["worker_build_mode"] == "rollback_hold_only"
        assert guard_accepts(database.rows[-1], worker_maintenance, proxy_mode=True)
        assert_quiescent(worker_maintenance)
        assert statuses == ["rollback-only hold"]
        assert database.violations == []
        validate_trial.assert_not_called()
        for factory in forbidden_factories.values():
            factory.assert_not_called()

        handlers[worker_main.signal.SIGTERM](worker_main.signal.SIGTERM, None)
        await asyncio.wait_for(running, 1)
        assert database.rows[-1]["status"] == "stopped"
        assert statuses == ["rollback-only hold", "stopping"]
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
