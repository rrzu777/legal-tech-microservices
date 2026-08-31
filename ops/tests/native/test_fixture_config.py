import importlib.util
import ast
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('native_fixture', Path(__file__).with_name('fixture.py'))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
OPS = Path(__file__).resolve().parents[2]


class FixtureConfigTests(unittest.TestCase):
    def test_dummy_environment_obeys_local_storage_and_idle_contract(self):
        values = fixture.fixture_values('# comment\nAPI_KEY\nCOOKIE_STORE_PATH\n')
        self.assertEqual(values['COOKIE_STORE_PATH'], '/var/lib/estrado-pjud/cookies.json')
        self.assertEqual(values['PJUD_PROCESS_OUTSIDE_OFFICE_HOURS'], 'false')
        self.assertEqual(values['PJUD_OFF_HOURS_VALIDATION_ONCE'], 'false')
        self.assertEqual(values['OJV_PROXY_URL'], '')
        self.assertEqual(values['API_KEY'], 'native-fixture-only')
        self.assertNotIn('# comment', values)

    def native_environment(self):
        self.assertTrue(hasattr(fixture, 'fixture_environment'), 'Complete native RG/WM environment builder missing')
        return {'PATH': '/usr/bin:/bin', **fixture.fixture_environment(
            (OPS / 'resource-guards.sh').read_text(), worker_uid=991, worker_gid=992)}

    def boundary(self, env, *, delegate=False):
        # Run the actual guard initialization through its RG/WM equality gate,
        # stopping before locks, preflight, systemd, file mutation or networking.
        source = (OPS / 'resource-guards.sh').read_text().split("\ntemp_dir=''", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(OPS / 'worker-maintenance.sh', root / 'worker-maintenance.sh')
            if delegate:
                provision = (OPS / 'provision.sh').read_text()
                start = provision.index('  source ')
                end = provision.index('  trap wm_close EXIT', start)
                child = root / 'provision-init.sh'
                child.write_text('set -euo pipefail\n' + provision[start:end] +
                                 'printf "%s\\0" "$wm_test" "$wm_delegated" "$wm_python" "${wm_args[@]}"\n')
                source += '\nwm_global_fd=8\nwm_admission_fd=9\nwm_operation_id=71ae117a-610b-46da-9766-3841100f8710\nwm_identity=fixture-identity\n'
                source += 'PROV_ENABLE_PJUD_WORKER=0 PROV_SKIP_CADDY=1 wm_delegate /bin/bash ' + shlex.quote(str(child))
            else:
                source += '\nprintf "%s\\0" "$wm_test" "$wm_delegated" "$wm_python" "${wm_args[@]}"\n'
            script = root / 'guard-init.sh'
            script.write_text(source)
            return subprocess.run(['/bin/bash', str(script), 'apply', '--expected-sha', 'a' * 40],
                                  env=env, capture_output=True, text=True, timeout=5)

    def test_generated_native_boundary_uses_complete_real_cli_arguments(self):
        env = self.native_environment()
        result = self.boundary(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split('\0')[:-1], [
            '1', '0', '/usr/bin/python3', '--test-mode',
            '--control-dir', '/var/lib/worker-maintenance', '--ack-dir', '/run/worker-maintenance',
            '--proc-root', '/proc', '--systemctl', '/usr/bin/systemctl',
            '--global-lock', '/run/lock/legaltech-resource-guards.lock',
            '--journal-root', '/var/lib/worker-maintenance-operations',
            '--health-url', 'http://127.0.0.1:8000/api/v1/health',
            '--root-uid', '0', '--root-gid', '0', '--worker-uid', '991', '--worker-gid', '992',
        ])
        self.assertEqual({key: env[key] for key in
                          ('WM_FLOCK', 'WM_DATE', 'WM_SLEEP', 'WM_POLL_ATTEMPTS', 'WM_POLL_SECONDS')},
                         {'WM_FLOCK': '/usr/bin/flock', 'WM_DATE': '/usr/bin/date',
                          'WM_SLEEP': '/usr/bin/sleep', 'WM_POLL_ATTEMPTS': '900', 'WM_POLL_SECONDS': '1'})

    def test_generated_native_boundary_rejects_every_missing_wm_override(self):
        complete = self.native_environment()
        for key in (key for key in complete if key.startswith('WM_') and key != 'WM_TEST_MODE'):
            with self.subTest(missing=key):
                partial = dict(complete)
                del partial[key]
                result = self.boundary(partial)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
        for changed in ({'WM_TEST_MODE': '0'}, {'RG_TEST_MODE': '0'}):
            with self.subTest(changed=changed):
                result = self.boundary(complete | changed)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')

    @unittest.skipUnless(os.geteuid() == 0, 'Default WM production mode requires root, as in the native guest')
    def test_rg_test_with_absent_wm_boundary_reproduces_native_exit_two(self):
        env = {key: value for key, value in self.native_environment().items() if not key.startswith('WM_')}
        result = self.boundary(env)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (2, '', ''))

    def test_provision_delegation_inherits_complete_native_wm_boundary(self):
        env = self.native_environment()
        parent = self.boundary(env)
        child = self.boundary(env, delegate=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        expected = parent.stdout.split('\0')
        expected[1] = '1'
        self.assertEqual(child.stdout.split('\0'), expected)

    def test_fixture_helper_initializes_the_same_complete_native_boundary(self):
        env = self.native_environment()
        tree = ast.parse(Path(__file__).with_name('exercise.py').read_text())
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == 'helper_death_and_closed_restart')
        body = next(ast.literal_eval(node.value) for node in helper.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'body' for target in node.targets))
        startup = body.split('wm_acquire_global', 1)[0]
        startup = startup.replace('/opt/legal-tech-microservices/ops/worker-maintenance.sh',
                                  shlex.quote(str(OPS / 'worker-maintenance.sh')))
        startup += 'printf "%s\\0" "$wm_test" "$wm_delegated" "$wm_python" "${wm_args[@]}"\n'
        helper_result = subprocess.run(['/bin/bash', '-c', startup], env=env,
                                       capture_output=True, text=True, timeout=5)
        self.assertEqual(helper_result.returncode, 0, helper_result.stderr)
        self.assertEqual(helper_result.stdout, self.boundary(env).stdout)
