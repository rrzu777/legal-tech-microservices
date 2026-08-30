"""Read-only unit tests for host admission and scoped cleanup; no Docker calls."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_runner', Path(__file__).with_name('run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def result(payload, rc=0):
    return subprocess.CompletedProcess([], rc, json.dumps(payload), '')


class RunnerTests(unittest.TestCase):
    def test_endpoint_override_fails_before_docker(self):
        with patch.dict(runner.os.environ, {'DOCKER_HOST': 'ssh://some-server'}, clear=True), \
                patch.object(runner.platform, 'system', return_value='Darwin'), \
                patch.object(runner, 'execute') as execute:
            with self.assertRaises(RuntimeError):
                runner.local_docker()
            execute.assert_not_called()

    def test_remote_active_context_is_read_only_rejected(self):
        with patch.dict(runner.os.environ, {}, clear=True), \
                patch.object(runner.platform, 'system', return_value='Darwin'), \
                patch.object(runner, 'execute', return_value=result([
                    {'Endpoints': {'docker': {'Host': 'ssh://remote'}}}])) as execute:
            with self.assertRaises(RuntimeError):
                runner.local_docker()
            self.assertEqual(execute.call_count, 1)

    def test_local_endpoint_is_pinned(self):
        with patch.dict(runner.os.environ, {}, clear=True), \
                patch.object(runner.platform, 'system', return_value='Darwin'), \
                patch.object(runner, 'execute', side_effect=[result([
                    {'Endpoints': {'docker': {'Host': 'unix:///var/run/docker.sock'}}}]),
                    result({'OperatingSystem': 'Docker Desktop', 'Architecture': 'aarch64'})]):
            self.assertEqual(runner.local_docker(), ['docker', '--host', 'unix:///var/run/docker.sock'])

    def test_cleanup_refuses_someone_elses_container(self):
        with patch.object(runner, 'execute', return_value=result([
                {'Config': {'Labels': {'native-validation-owner': 'not-us'}}}])) as execute:
            self.assertFalse(runner.cleanup('exact-name', 'us'))
            self.assertEqual(execute.call_count, 1)

    def test_stop_timeout_still_removes_owned_container(self):
        with patch.object(runner, 'execute', side_effect=[result([
                {'Config': {'Labels': {'native-validation-owner': 'us'}}}]),
                subprocess.TimeoutExpired('stop', 20), result({})]) as execute:
            # Report the timeout conservatively even when rm succeeded.
            self.assertFalse(runner.cleanup('exact-name', 'us'))
            self.assertEqual(execute.call_args.args[-3:], ('rm', '--force', 'exact-name'))

    def test_inspect_error_is_not_successful_cleanup(self):
        with patch.object(runner, 'execute', return_value=result({}, rc=1)) as execute:
            self.assertFalse(runner.cleanup('exact-name', 'us'))
            self.assertEqual(execute.call_count, 1)


if __name__ == '__main__':
    unittest.main()
