"""Host safety regressions for the native accelerated laboratory."""
import importlib.util
from pathlib import Path
import tempfile
import tarfile
import hashlib
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch


class HvfTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('run_hvf.py')
        self.assertTrue(path.is_file(), 'Native HVF runner is missing')
        spec = importlib.util.spec_from_file_location('hvf', path)
        self.hvf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hvf)

    def test_isolated_command_has_only_loopback_forward_and_no_host_share(self):
        args = self.hvf.qemu_arguments(Path('/private/tmp/native-example'), 32123, isolated=True)
        network = args[args.index('-netdev') + 1]
        self.assertEqual(network, 'user,id=net0,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:32123-:22')
        self.assertNotIn('-virtfs', args)
        self.assertNotIn('-fsdev', args)
        self.assertEqual(args[args.index('-accel') + 1], 'hvf')
        self.assertEqual(args[args.index('-m') + 1], '4096')
        self.assertEqual(args[args.index('-smp') + 1], '2')

    def test_bootstrap_is_explicit_separate_network_mode(self):
        args = self.hvf.qemu_arguments(Path('/private/tmp/native-example'), 32123, isolated=False)
        self.assertIn('restrict=off', args[args.index('-netdev') + 1])

    def test_payload_rejects_linked_parent_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'ops').mkdir()
            (root / 'ops' / 'safe.py').touch()
            self.hvf.validate_payload(root, ['ops/safe.py'])
            (root / 'ops' / 'alias').symlink_to(root / 'ops')
            for item in ('ops/alias/safe.py', 'ops/.env', '../secret'):
                with self.subTest(item=item), self.assertRaises(RuntimeError):
                    self.hvf.validate_payload(root, [item])

    def test_payload_includes_exact_stdlib_worker_and_untracked_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            expected = {
                'estrado-pjud-service/worker/__init__.py',
                'estrado-pjud-service/worker/maintenance.py',
                'estrado-pjud-service/worker/maintenance_store.py',
                'estrado-pjud-service/worker/sd_notify.py',
                'estrado-pjud-service/worker/maintenance_heartbeat.py',
                'ops/tests/native/fixture.py', 'ops/tests/native/fixture_worker.py',
                'ops/tests/native/exercise.py', 'ops/tests/native/probe.py',
                'ops/tracked.py',
            }
            for relative in expected | {'estrado-pjud-service/.env',
                                        'estrado-pjud-service/worker/__main__.py'}:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            self.assertTrue(hasattr(self.hvf, 'payload_files'), 'Worker payload builder missing')
            with patch.object(self.hvf, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, 'ops/tracked.py\0', '')):
                paths = self.hvf.payload_files(root)
            self.assertEqual(set(paths), expected)
            for relative in ('estrado-pjud-service/.env', 'estrado-pjud-service/worker/__main__.py',
                             'estrado-pjud-service/app/__init__.py', 'ops/secrets/.env',
                             str(root / 'ops/tracked.py')):
                with self.subTest(relative=relative), self.assertRaises(RuntimeError):
                    self.hvf.validate_payload(root, [relative])
            (root / 'estrado-pjud-service/worker/sd_notify.py').unlink()
            (root / 'estrado-pjud-service/worker/sd_notify.py').symlink_to(root / 'ops/tracked.py')
            with self.assertRaises(RuntimeError):
                self.hvf.validate_payload(root, ['estrado-pjud-service/worker/sd_notify.py'])

    def test_cleanup_refuses_unowned_directory(self):
        with tempfile.TemporaryDirectory(prefix='resource-guards-hvf-') as directory:
            path = Path(directory).resolve()
            (path / 'keep').touch()
            (path / 'owner').write_text('someone-else')
            with self.assertRaises(RuntimeError):
                self.hvf.clean_workspace(path, 'unknown')
            self.assertTrue((path / 'keep').is_file())

    def test_pressure_guard_stops_owned_guest_after_boot(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])
        try:
            with patch.object(self.hvf, 'pressure_ok', return_value=False):
                self.hvf.watch_guest(process, threading.Event())
            self.assertIsNotNone(process.wait(timeout=2))
        finally:
            if process.poll() is None:
                process.kill(); process.wait()

    def test_generated_free_admission_is_executable_and_rejects_other_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'environment.json').write_text('{}')
            setup = self.hvf.small_guest_setup()
            body = '\n'.join(setup.splitlines()[1:-1]).replace('/opt/native-fixture', str(root))
            subprocess.run([sys.executable, '-c', body], check=True)
            good = subprocess.run([str(root / 'free-admission'), '-b'], capture_output=True, text=True)
            self.assertEqual(good.returncode, 0)
            self.assertIn('Mem: 8589934592', good.stdout)
            bad = subprocess.run([str(root / 'free-admission'), '-m'], capture_output=True)
            self.assertEqual(bad.returncode, 2)

    def test_media_archive_preserves_case_dots_bytes_and_modes(self):
        self.assertTrue(hasattr(self.hvf, 'make_payload_media'), 'Exact archive transport is missing')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); payload = root / 'payload'; media = root / 'media'
            payload.mkdir(); media.mkdir()
            relative = 'ops/systemd-templates/hermes-user.slice.conf'
            original = payload / relative
            original.parent.mkdir(parents=True)
            original.write_text('fixture content\n'); original.chmod(0o644)
            (payload / 'README.md').write_text('case sensitive')
            digest = self.hvf.make_payload_media(payload, media)
            self.assertEqual(hashlib.sha256((media / 'manifest.json').read_bytes()).hexdigest(), digest)
            manifest = json.loads((media / 'manifest.json').read_text())
            self.assertIn(relative, manifest)
            self.assertIn('README.md', manifest)
            self.assertEqual(manifest[relative]['mode'], 0o644)
            with tarfile.open(media / 'payload.tar') as archive:
                archive.extractall(root / 'roundtrip', filter='data')
            self.assertEqual((root / 'roundtrip' / relative).read_bytes(), original.read_bytes())
            self.assertTrue((root / 'roundtrip' / 'README.md').is_file())

    def test_heartbeat_payload_roundtrips_hashed_and_imports_without_site_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, media = root / 'payload', root / 'media'
            payload.mkdir(); media.mkdir()
            for relative in self.hvf.WORKER_PAYLOAD:
                original = self.hvf.ROOT / relative
                target = payload / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original.read_bytes())
            self.hvf.validate_payload(payload, list(self.hvf.WORKER_PAYLOAD))
            self.hvf.make_payload_media(payload, media)
            relative = 'estrado-pjud-service/worker/maintenance_heartbeat.py'
            manifest = json.loads((media / 'manifest.json').read_text())
            expected = (self.hvf.ROOT / relative).read_bytes()
            self.assertEqual(manifest[relative]['sha256'], hashlib.sha256(expected).hexdigest())
            with tarfile.open(media / 'payload.tar') as archive:
                archive.extractall(root / 'roundtrip', filter='data')
            self.assertEqual((root / 'roundtrip' / relative).read_bytes(), expected)
            result = subprocess.run([sys.executable, '-I', '-S', '-c',
                'import sys; sys.path.insert(0, sys.argv[1]); '
                'from worker.maintenance_heartbeat import maintenance_proof; '
                'assert maintenance_proof(None, initialization_started=False) is None',
                str(root / 'roundtrip/estrado-pjud-service')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
