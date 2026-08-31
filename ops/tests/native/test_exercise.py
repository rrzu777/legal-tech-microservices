"""Pure snapshot regressions; no VM, systemctl or network access."""
import importlib.util
import ast
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_exercise', Path(__file__).with_name('exercise.py'))
exercise = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exercise)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_absent_path(self):
        self.assertIsNone(exercise.inspect_path(self.root / 'absent'))

    def test_dangling_symlink_is_not_absence(self):
        path = self.root / 'dangling'
        path.symlink_to(self.root / 'absent')
        with self.assertRaises(RuntimeError):
            exercise.inspect_path(path)

    def test_dangling_ancestor_is_not_absence(self):
        path = self.root / 'parent'
        path.symlink_to(self.root / 'absent')
        with self.assertRaises(RuntimeError):
            exercise.inspect_path(path / 'file')

    def test_file_type_and_digest(self):
        path = self.root / 'file'
        path.write_text('fixture')
        metadata = exercise.inspect_path(path)
        self.assertEqual(metadata[0], stat.S_IFREG)
        self.assertEqual(len(metadata[-1]), 64)
        self.assertEqual(exercise.inspect_path(self.root)[0], stat.S_IFDIR)

    def test_credentials_and_swap_metadata_never_read_contents(self):
        for name in ('.env', 'legaltech-monitoring.env', 'swapfile'):
            path = self.root / name
            path.touch()
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('content read')):
                metadata = exercise.inspect_path(path, hash_contents=name != 'swapfile')
            self.assertIsNotNone(metadata)
            self.assertIsNone(metadata[-1])

    def test_provision_capture_preserves_arguments_environment_and_failure(self):
        tree = ast.parse(Path(__file__).with_name('exercise.py').read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'write_text'
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == 'provision_capture']
        self.assertEqual(len(calls), 1)
        body = ast.literal_eval(calls[0].args[0])
        real = self.root / 'provision.sh'
        real.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$PROV_SKIP_CADDY" "$1"\nexit 7\n')
        real.chmod(0o755)
        log = self.root / 'provision.log'
        wrapper = self.root / 'capture'
        wrapper.write_text(body.replace('/opt/legal-tech-microservices/ops/provision.sh', str(real))
                          .replace('/opt/native-fixture/provision.log', str(log)))
        wrapper.chmod(0o755)
        result = subprocess.run([str(wrapper), '--fixture'], env={'PROV_SKIP_CADDY': '1'}, capture_output=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(log.read_text(), '1|--fixture\n')
        self.assertEqual(result.stdout, b'')

    def test_identity_proof_rejects_wrapper_pid_stale_ack_and_nonce_reuse(self):
        self.assertTrue(hasattr(exercise, 'verify_identity'), 'Native exact identity proof missing')
        boot, operation, nonce = [str(uuid.uuid4()) for _ in range(3)]
        identity = f'{boot}:512:9012:{nonce}'
        ack = dict(boot_id=boot, pid=512, start_ticks=9012, instance_id=nonce,
                   operation_id=operation, state='quiescent', inflight=0)
        status = f'hold {operation} {identity}'
        self.assertEqual(exercise.verify_identity(status, ack, 512), identity)
        with self.assertRaises(AssertionError):
            exercise.verify_identity(status, ack, 511)  # xvfb-run shell, not worker
        for update in ({'pid': 513}, {'operation_id': str(uuid.uuid4())},
                       {'instance_id': str(uuid.uuid4())}, {'start_ticks': 9013},
                       {'state': 'draining'}, {'inflight': 1}):
            with self.subTest(update=update), self.assertRaises(AssertionError):
                exercise.verify_identity(status, ack | update, 512)
        for previous in (identity, f'{boot}:511:9000:{nonce}',
                         f'{boot}:512:9012:{uuid.uuid4()}'):
            with self.subTest(previous=previous), self.assertRaises(AssertionError):
                exercise.verify_identity(status, ack, 512, previous=previous)
        self.assertEqual(exercise.verify_identity(status, ack, 512,
                         previous=f'{boot}:511:9000:{uuid.uuid4()}'), identity)

    def test_guard_observer_runs_while_helper_lives_and_preserves_failure(self):
        self.assertTrue(hasattr(exercise, 'run_observed'), 'Concurrent native observation missing')
        marker = self.root / 'continue'
        body = 'import pathlib,time,sys; p=pathlib.Path(sys.argv[1]); print("start",flush=True)\nwhile not p.exists(): time.sleep(.01)\nprint("fault",file=sys.stderr); sys.exit(7)'
        def during(process):
            self.assertIsNone(process.poll())
            marker.touch()
        result = exercise.run_observed(sys.executable, '-c', body, str(marker), during=during)
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr.strip()), (7, 'start', 'fault'))

    def test_failed_observer_reaps_only_its_owned_helper(self):
        self.assertTrue(hasattr(exercise, 'run_observed'), 'Owned helper cleanup missing')
        processes = []
        def during(process):
            processes.append(process)
            raise AssertionError('fixture assertion')
        with self.assertRaisesRegex(AssertionError, 'fixture assertion'):
            exercise.run_observed(sys.executable, '-c', 'import time; time.sleep(30)', during=during)
        self.assertIsNotNone(processes[0].poll())


class WorkerProofTests(unittest.TestCase):
    """Real proof/polling logic; only Linux/systemd observation inputs are fake."""
    def setUp(self):
        self.identity = 'f784c8bd-67c3-448e-ae1c-55ac6feab947:512:9012:bf763d76-b99c-464d-80d8-bcbd9520b923'
        self.operation = '71ae117a-610b-46da-9766-3841100f8710'
        self.ack = dict(version=1, operation_id=self.operation,
                        boot_id='f784c8bd-67c3-448e-ae1c-55ac6feab947', pid=512,
                        start_ticks=9012, instance_id='bf763d76-b99c-464d-80d8-bcbd9520b923',
                        state='draining', inflight=0)
        self.observations = []
        self.elapsed = 0

    def observe(self, frames):
        current = {}
        def run(*args, **kwargs):
            if args == (sys.executable, str(exercise.CLI), 'status'):
                current.clear()
                current.update(frames[min(len(self.observations), len(frames) - 1)])
                self.observations.append(dict(current))
                status = f'{current.get("control", "open")} {self.operation} {self.identity}\n'
                return subprocess.CompletedProcess(args, 0, status, '')
            self.assertEqual(args, ('systemctl', 'show', 'estrado-pjud-worker.service', '--property=MainPID', '--value'))
            return subprocess.CompletedProcess(args, 0, str(current.get('mainpid', 512)), '')
        def read_text(path, *args, **kwargs):
            if str(path) == '/run/worker-maintenance/ack.json':
                return json.dumps(self.ack | current.get('ack', {}))
            self.assertEqual(str(path), '/proc/512/stat')
            return '512 (python3) S 511'
        def read_bytes(path):
            if str(path) == '/proc/512/cmdline':
                return b'/usr/bin/python3\0/opt/native-fixture/fixture_worker.py\0'
            self.assertEqual(str(path), '/proc/511/cmdline')
            return b'/bin/sh\0/usr/bin/xvfb-run\0-a\0'
        def sleep(seconds):
            self.elapsed += seconds
        self.enterContext(patch.object(exercise, 'run', side_effect=run))
        self.enterContext(patch.object(Path, 'read_text', read_text))
        self.enterContext(patch.object(Path, 'read_bytes', read_bytes))
        self.enterContext(patch.object(exercise.time, 'monotonic', side_effect=lambda: self.elapsed))
        self.enterContext(patch.object(exercise.time, 'sleep', side_effect=sleep))

    def test_prove_worker_waits_for_quiescent_to_draining_after_release(self):
        self.observe([{'ack': {'state': 'quiescent'}}, {'ack': {'state': 'draining'}}])
        self.assertEqual(exercise.prove_worker('open'), self.identity)
        self.assertEqual(len(self.observations), 2)
        self.assertEqual(self.elapsed, 0.05)

    def test_prove_worker_waits_for_expected_control_and_matching_ack(self):
        self.observe([{'control': 'hold', 'ack': {'state': 'quiescent'}},
                      {'control': 'open', 'ack': {'state': 'quiescent'}},
                      {'control': 'open', 'ack': {'state': 'draining'}}])
        self.assertEqual(exercise.prove_worker('open'), self.identity)
        self.assertEqual(len(self.observations), 3)

    def test_prove_worker_persistent_ack_mismatch_exhausts_deadline(self):
        self.observe([{'ack': {'state': 'quiescent'}}])
        with self.assertRaisesRegex(AssertionError, 'Bounded native fixture observation timed out'):
            exercise.prove_worker('open')
        self.assertGreater(len(self.observations), 1)
        self.assertGreaterEqual(self.elapsed, 20)
        self.assertLess(self.elapsed, 20.06)

    def test_prove_worker_persistent_identity_mismatch_never_returns_proof(self):
        self.observe([{'ack': {'instance_id': 'f55b7b7e-418e-4632-bbd1-462ec711c016'}}])
        with self.assertRaisesRegex(AssertionError, 'Bounded native fixture observation timed out'):
            exercise.prove_worker('open')
        self.assertGreaterEqual(self.elapsed, 20)


if __name__ == '__main__':
    unittest.main()
