"""Shared test configuration."""
import pytest
import os
from dataclasses import replace
from uuid import uuid4

from app.metrics import api_metrics


@pytest.fixture
def worker_maintenance(tmp_path):
    """Real local protocol; no production paths, identity or environment bypass."""
    from worker.maintenance import WorkerMaintenance
    from worker.maintenance_store import MaintenanceStore, ProcessIdentity, StorePolicy
    control_dir, ack_dir = tmp_path.resolve() / "control", tmp_path.resolve() / "ack"
    control_dir.mkdir(mode=0o750)
    ack_dir.mkdir(mode=0o700)
    control_dir.chmod(0o750)
    ack_dir.chmod(0o700)
    (control_dir / "admission.lock").touch(mode=0o640)
    (control_dir / "admission.lock").chmod(0o640)
    for path in (control_dir, ack_dir, control_dir / "admission.lock"):
        os.chown(path, -1, os.getgid())
    store = MaintenanceStore(control_dir, ack_dir,
                             StorePolicy(os.getuid(), os.getgid(), os.getuid(), os.getgid()),
                             allow_control_writes=True)
    held = store.initialize_hold(str(uuid4()))
    store.transition(held.operation_id, "hold", replace(held, state="open"))
    return WorkerMaintenance(store, ProcessIdentity(str(uuid4()), os.getpid(), 42, str(uuid4())))


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset slowapi rate limiter storage before each test."""
    from app.rate_limit import limiter
    limiter.reset()


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset API metrics before each test."""
    api_metrics.reset()
