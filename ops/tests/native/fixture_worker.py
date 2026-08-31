"""Local controllable work using production admission and notification modules.

Only request/result and heartbeat files are fixture data. ACKs are exclusively
published by WorkerMaintenance. No application/browser/remote client is imported.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, '/opt/legal-tech-microservices/estrado-pjud-service')
from worker.maintenance import WorkerMaintenance
from worker.maintenance_store import AdmissionClosed, MaintenanceStore, ProcessIdentity
from worker.sd_notify import notify_ready, notify_watchdog


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


async def serve(worker, logs, shutdown, *, poll_seconds=0.05):
    release = asyncio.Event()
    operation = None
    last_request = None
    worker.publish_ack()
    notify_ready()
    next_watchdog = 0

    def result(request, outcome):
        write_json(logs / 'result.json', {'id': request, 'outcome': outcome, 'pid': os.getpid()})

    async def admitted(request):
        async def body():
            result(request, 'started')
            await release.wait()
        try:
            await worker.run(body)
        except AdmissionClosed:
            result(request, 'blocked')

    async def no_effect():
        return None

    try:
        while not shutdown.is_set():
            worker.publish_ack()
            write_json(logs / 'heartbeat.json', [{
                'status': 'maintenance' if worker.store.read_control().state == 'hold' else 'idle_off_hours',
                'last_heartbeat_at': datetime.now(timezone.utc).isoformat(),
                'metadata': {'process_outside_office_hours_enabled': False, 'mint_attempts': 0},
            }])
            if asyncio.get_running_loop().time() >= next_watchdog:
                notify_watchdog()
                next_watchdog = asyncio.get_running_loop().time() + 1
            request_path = logs / 'request.json'
            if request_path.is_file():
                request = json.loads(request_path.read_text())
                if request['id'] != last_request:
                    last_request = request['id']
                    if request['action'] == 'start':
                        if operation is not None and not operation.done():
                            raise RuntimeError('Fixture operation already active')
                        release.clear()
                        operation = asyncio.create_task(admitted(last_request))
                    elif request['action'] == 'release':
                        release.set()
                        if operation is not None:
                            await operation
                        result(last_request, 'released')
                    elif request['action'] == 'probe':
                        try:
                            await worker.run(no_effect)
                        except AdmissionClosed:
                            result(last_request, 'blocked')
                        else:
                            result(last_request, 'admitted')
                    else:
                        raise RuntimeError('Unknown fixture action')
            try:
                await asyncio.wait_for(shutdown.wait(), poll_seconds)
            except TimeoutError:
                pass
    finally:
        release.set()
        if operation is not None:
            await operation


async def main():
    worker = WorkerMaintenance(MaintenanceStore.production(), ProcessIdentity.current())
    shutdown = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, shutdown.set)
    await serve(worker, Path('/opt/legal-tech-microservices/estrado-pjud-service/logs'), shutdown)


if __name__ == '__main__':
    asyncio.run(main())
