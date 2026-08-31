"""Stdlib-only heartbeat projection; never a substitute for operator ACK/EX."""
from worker.maintenance_store import MaintenanceError


def maintenance_proof(worker, *, initialization_started: bool):
    if worker is None:
        return None
    try:
        ack = worker.publish_ack()
    except MaintenanceError:
        return None
    if ack.state != "quiescent" or ack.inflight != 0:
        return None
    return {
        "version": 1,
        "operation_id": ack.operation_id,
        "identity": f"{ack.boot_id}:{ack.pid}:{ack.start_ticks}:{ack.instance_id}",
        "state": "quiescent",
        "inflight": 0,
        "startup_blocked": initialization_started is False,
    }
