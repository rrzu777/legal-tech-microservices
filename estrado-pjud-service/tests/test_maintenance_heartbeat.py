"""Production Metrics/ProxyControl rows must satisfy the actual guard jq."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from worker.metrics import Metrics
from worker.proxy_control import ProxyControl, ProxyControlSnapshot
from tests.test_maintenance_wiring import hold

ROOT = Path(__file__).resolve().parents[2]


def guard_accepts(payload, worker, *, proxy_mode):
    source = (ROOT / "ops/resource-guards.sh").read_text()
    predicate = re.search(r"--argjson require_zero_mint[^\n]* '(.*?)\n  ' \"\$body\"", source, re.S).group(1)
    identity = worker.identity
    row = {key: payload[key] for key in ("status", "last_heartbeat_at", "metadata")}
    result = subprocess.run([shutil.which("jq") or "/usr/bin/jq", "-er",
        "--argjson", "proxy_mode", str(int(proxy_mode)), "--argjson", "require_zero_mint", "1",
        "--arg", "maintenance_operation", worker.store.read_control().operation_id,
        "--arg", "maintenance_identity", f"{identity.boot_id}:{identity.pid}:{identity.start_ticks}:{identity.instance_id}",
        predicate], input=json.dumps([row]), text=True, capture_output=True)
    assert result.returncode in (0, 4), result.stderr
    return result.returncode == 0


def metrics_for(worker, proxy_mode):
    config = SimpleNamespace(WORKER_ID="test", POOL_SIZE=1, PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=False)
    control = ProxyControl(MagicMock()) if proxy_mode else None
    metrics = Metrics(config, MagicMock(), proxy_control=control, maintenance=worker)
    metrics.set_status("paused")
    return metrics, control


@pytest.mark.parametrize("proxy_mode", [False, True])
def test_real_cold_closed_heartbeat_passes_guard(worker_maintenance, proxy_mode):
    metrics, _ = metrics_for(worker_maintenance, proxy_mode)
    hold(worker_maintenance)
    row = metrics.heartbeat_payload()
    assert row["status"] == "paused"
    assert guard_accepts(row, worker_maintenance, proxy_mode=proxy_mode)


@pytest.mark.parametrize("proxy_mode", [False, True])
def test_generic_pause_open_hold_uncertain_and_live_work_are_not_proof(worker_maintenance, proxy_mode):
    metrics, _ = metrics_for(worker_maintenance, proxy_mode)
    assert not guard_accepts(metrics.heartbeat_payload(), worker_maintenance, proxy_mode=proxy_mode)
    hold(worker_maintenance)
    worker_maintenance.mark_uncertain()
    assert not guard_accepts(metrics.heartbeat_payload(), worker_maintenance, proxy_mode=proxy_mode)


@pytest.mark.parametrize("snapshot", [
    ProxyControlSnapshot(False, "unavailable", "query_failed", None, "local"),
    ProxyControlSnapshot(False, "unavailable", "not_loaded", 1, "local"),
    ProxyControlSnapshot(False, "unavailable", "not_loaded", None, "database"),
    ProxyControlSnapshot(False, "paused", "operator", 1, "database"),
    ProxyControlSnapshot(False, "unknown", None, None, "local"),
])
def test_real_proxy_unknown_never_uses_cold_start_exception(worker_maintenance, snapshot):
    metrics, control = metrics_for(worker_maintenance, True)
    control._snapshot = snapshot
    hold(worker_maintenance)
    assert not guard_accepts(metrics.heartbeat_payload(), worker_maintenance, proxy_mode=True)


def test_not_loaded_after_initialization_attempt_is_not_cold_proof(worker_maintenance):
    metrics, _ = metrics_for(worker_maintenance, True)
    metrics.initialization_started = True
    hold(worker_maintenance)
    assert not guard_accepts(metrics.heartbeat_payload(), worker_maintenance, proxy_mode=True)


@pytest.mark.parametrize("field,value", [
    ("operation_id", "stale"), ("identity", "stale"), ("version", 2),
    ("inflight", 1), ("state", "draining"), ("startup_blocked", "true"),
])
def test_maintenance_proof_identity_and_schema_are_checked(worker_maintenance, field, value):
    metrics, _ = metrics_for(worker_maintenance, True)
    hold(worker_maintenance)
    row = metrics.heartbeat_payload()
    assert guard_accepts(row, worker_maintenance, proxy_mode=True)
    row["metadata"]["maintenance"][field] = value
    assert not guard_accepts(row, worker_maintenance, proxy_mode=True)


async def test_live_admission_is_never_projected_as_quiescent(worker_maintenance):
    metrics, _ = metrics_for(worker_maintenance, False)
    async def body():
        hold(worker_maintenance)
        assert not guard_accepts(metrics.heartbeat_payload(), worker_maintenance, proxy_mode=False)
    await worker_maintenance.run(body)
    assert guard_accepts(metrics.heartbeat_payload(), worker_maintenance, proxy_mode=False)
