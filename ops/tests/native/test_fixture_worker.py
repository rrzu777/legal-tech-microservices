"""Behavioral fixture contract, real store/flock/admission/notifier, no systemd."""
import asyncio
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'estrado-pjud-service'))
from worker.maintenance import WorkerMaintenance
from worker.maintenance_store import AdmissionClosed, MaintenanceStore, ProcessIdentity, StorePolicy
from worker import sd_notify


class RealWorkerFixtureTests(unittest.TestCase):
    def test_admitted_operation_drains_and_late_work_is_blocked_with_real_ack(self):
        path = Path(__file__).with_name('fixture_worker.py')
        self.assertTrue(path.is_file(), 'Real maintenance fixture is missing')
        spec = importlib.util.spec_from_file_location('fixture_worker_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='native-worker-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            for name, mode in (('control', 0o750), ('ack', 0o700), ('logs', 0o700)):
                (root / name).mkdir(mode=mode)
                (root / name).chmod(mode)
                os.chown(root / name, os.getuid(), os.getgid())
            lock = root / 'control/admission.lock'
            lock.touch(mode=0o640)
            os.chown(lock, os.getuid(), os.getgid())
            store = MaintenanceStore(root / 'control', root / 'ack',
                                     StorePolicy(os.getuid(), os.getgid(), os.getuid(), os.getgid()),
                                     allow_control_writes=True)
            initial = store.initialize_hold(str(uuid.uuid4()))
            store.transition(initial.operation_id, 'hold', replace(initial, state='open'))
            identity = (ProcessIdentity.current() if sys.platform == 'linux' else
                        ProcessIdentity(str(uuid.uuid4()), os.getpid(), 42, str(uuid.uuid4())))
            worker = WorkerMaintenance(store, identity)
            async def scenario():
                shutdown = asyncio.Event()
                running = asyncio.create_task(module.serve(worker, root / 'logs', shutdown, poll_seconds=0.01))
                async def command(action):
                    request = str(uuid.uuid4())
                    module.write_json(root / 'logs/request.json', {'id': request, 'action': action})
                    async with asyncio.timeout(2):
                        while True:
                            result = root / 'logs/result.json'
                            if result.is_file():
                                value = json.loads(result.read_text())
                                if value['id'] == request:
                                    return value
                            await asyncio.sleep(0.01)
                try:
                    self.assertEqual((await command('start'))['outcome'], 'started')
                    current = store.read_control()
                    held = replace(current, state='hold', operation_id=str(uuid.uuid4()))
                    store.transition(current.operation_id, 'open', held)
                    self.assertEqual((await command('probe'))['outcome'], 'blocked')
                    ack = store.read_ack(expected_operation_id=held.operation_id, expected_identity=identity)
                    self.assertEqual((ack.state, ack.inflight), ('draining', 1))
                    with self.assertRaises(AdmissionClosed), store.exclusive_lease():
                        pass
                    self.assertEqual((await command('release'))['outcome'], 'released')
                    async with asyncio.timeout(2):
                        while worker.inflight:
                            await asyncio.sleep(0.01)
                    with store.exclusive_lease():
                        self.assertEqual(worker.publish_ack().state, 'quiescent')
                    self.assertFalse(worker.uncertain)
                    self.assertEqual(json.loads((root / 'logs/heartbeat.json').read_text())[0]['status'], 'maintenance')
                finally:
                    shutdown.set()
                    await asyncio.wait_for(running, 2)
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                address = directory + '/notify'
                server.bind(address)
                server.settimeout(0.1)
                with patch.object(sd_notify, '_socket_path', address):
                    asyncio.run(scenario())
                self.assertEqual(server.recv(8192), f'READY=1\nMAINPID={os.getpid()}'.encode())
