"""Pure snapshot regressions; no VM, systemctl or network access."""
import importlib.util
import ast
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
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


if __name__ == '__main__':
    unittest.main()
