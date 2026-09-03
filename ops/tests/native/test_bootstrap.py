"""Host gates for the opt-in, non-bootstrap-success native characterization."""
import asyncio
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import tempfile
import time
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BootstrapTransportTests(unittest.TestCase):
    def setUp(self):
        self.hvf = load('bootstrap_hvf', 'run_hvf.py')

    def test_daytime_flag_is_explicit_and_only_transported_to_characterization(self):
        args = self.hvf.parse_args(['--base-image', '/fixture/base.qcow2',
            '--mode', 'bootstrap-characterization', '--allow-daytime-lab'])
        self.assertTrue(args.allow_daytime_lab)
        self.assertEqual(self.hvf.stage_command('bootstrap_exercise.py', allow_daytime_lab=args.allow_daytime_lab),
            'sudo env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -u '
            '/mnt/payload/ops/tests/native/bootstrap_exercise.py --allow-daytime-lab')
        defaults = self.hvf.parse_args(['--base-image', '/fixture/base.qcow2'])
        self.assertFalse(defaults.allow_daytime_lab)
        self.assertEqual(self.hvf.stage_command('fixture.py'),
                         'sudo python3 -u /mnt/payload/ops/tests/native/fixture.py')
        self.assertFalse(self.hvf.stage_command('bootstrap_exercise.py').endswith('--allow-daytime-lab'))
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.hvf.parse_args(['--base-image', '/fixture/base.qcow2', '--allow-daytime-lab'])
        with self.assertRaises(ValueError):
            self.hvf.stage_command('exercise.py', allow_daytime_lab=True)

    def test_default_mode_still_runs_existing_fixture_without_new_dependencies(self):
        self.assertTrue(hasattr(self.hvf, 'parse_args'), 'explicit native mode parser missing')
        args = self.hvf.parse_args(['--base-image', '/fixture/base.qcow2'])
        self.assertEqual(self.hvf.native_stages(args.mode), ('probe.py', 'fixture.py', 'exercise.py'))
        config = self.hvf.cloud_config('synthetic-public-key', mode=args.mode)
        self.assertEqual(config['packages'], ['git', 'jq', 'curl', 'cron', 'xvfb', 'xauth', 'dbus-user-session'])
        self.assertNotIn('write_files', config)
        self.assertEqual(len(config['runcmd']), 1)

    def test_opt_in_mode_does_not_start_fixture_or_initialize_control(self):
        self.assertTrue(hasattr(self.hvf, 'parse_args'), 'explicit native mode parser missing')
        args = self.hvf.parse_args(['--base-image', '/fixture/base.qcow2', '--mode', 'bootstrap-characterization'])
        self.assertEqual(self.hvf.native_stages(args.mode), ('bootstrap_exercise.py',))

    def test_runtime_install_is_guest_only_hash_pinned_before_ready_marker(self):
        self.assertTrue(hasattr(self.hvf, 'cloud_config'), 'guest-only runtime config missing')
        config = self.hvf.cloud_config('synthetic-public-key', mode='bootstrap-characterization')
        self.assertEqual(config['packages'][-1], 'python3-venv')
        self.assertEqual(config['write_files'], [{
            'path': '/opt/native-runtime-requirements.txt', 'owner': 'root:root', 'permissions': '0644',
            'content': 'uvicorn==0.41.0 --hash=sha256:29e35b1d2c36a04b9e180d4007ede3bcb32a85fbdfd6c6aeb3f26839de088187\n'
                       'click==8.1.8 --hash=sha256:63c132bbbed01578a06712a2d1f497bb62d9c1c0d329b7903a866228027263b2\n'
                       'h11==0.16.0 --hash=sha256:63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86\n',
        }])
        commands = config['runcmd']
        self.assertEqual(commands[0], ['/usr/bin/python3', '-m', 'venv', '/opt/native-runtime'])
        self.assertEqual(commands[1], [
            '/opt/native-runtime/bin/python', '-m', 'pip', '--isolated', 'install',
            '--require-hashes', '--only-binary=:all:', '--no-deps', '--disable-pip-version-check',
            '--no-cache-dir', '--retries', '0', '--timeout', '20', '--index-url', 'https://pypi.org/simple',
            '-r', '/opt/native-runtime-requirements.txt',
        ])
        # Readiness must require an actual successful runtime/import check, not
        # merely completion of cloud-init's earlier commands after an error.
        self.assertIn('/opt/native-runtime/bin/python', commands[-1][-1])
        self.assertTrue(commands[-1][-1].endswith('touch /var/lib/native-ready'))

    def test_missing_required_bootstrap_module_rejects_payload_even_if_untracked(self):
        self.assertTrue(hasattr(self.hvf, 'BOOTSTRAP_PAYLOAD'), 'required bootstrap payload missing')
        required = {
            'ops/tests/native/bootstrap_exercise.py', 'ops/bootstrap-worker-maintenance.py',
            'ops/bootstrap-audit.py', 'ops/worker-maintenance.py',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            defaults = {'ops/tests/native/' + name for name in
                        ('fixture.py', 'fixture_worker.py', 'exercise.py', 'probe.py')}
            files = defaults | set(self.hvf.WORKER_PAYLOAD) | required
            for relative in files:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic payload')
            with patch.object(self.hvf, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
                self.assertEqual(set(self.hvf.payload_files(root)), defaults | set(self.hvf.WORKER_PAYLOAD))
                self.assertEqual(set(self.hvf.payload_files(root, mode='bootstrap-characterization')), files)
                for relative in required:
                    target = root / relative
                    target.unlink()
                    with self.subTest(missing=relative), self.assertRaises(RuntimeError):
                        self.hvf.payload_files(root, mode='bootstrap-characterization')
                    target.write_text('synthetic payload')


class DaytimeLabWindowTests(unittest.TestCase):
    def test_default_forwards_original_config_and_never_swallows_window_error(self):
        module = load('bootstrap_default_window', 'bootstrap_exercise.py')
        self.assertTrue(hasattr(module, 'lab_window'), 'explicit lab lifecycle window missing')
        config = object()
        calls = []
        def production_window(value):
            calls.append(value)
            raise RuntimeError('original production window refusal')
        with self.assertRaisesRegex(RuntimeError, 'original production window refusal'):
            module.lab_window(SimpleNamespace(window=production_window), config)
        self.assertEqual(calls, [config])

    def test_daytime_opt_in_requires_all_disposable_guest_identity_checks(self):
        module = load('bootstrap_daytime_window', 'bootstrap_exercise.py')
        self.assertTrue(hasattr(module, 'ALLOW_DAYTIME_LAB'), 'explicit lab opt-in missing')
        config = object()
        bootstrap = SimpleNamespace(window=lambda value: self.fail('opt-in must not monkeypatch/call production window'))
        with (patch.object(module, 'ALLOW_DAYTIME_LAB', True),
              patch.object(module.sys, 'platform', 'linux'), patch.object(module.os, 'geteuid', return_value=0),
              patch.object(module.Path, 'read_text', return_value='native-guards\n'),
              patch.object(module, 'run', return_value=subprocess.CompletedProcess([], 0, 'qemu\n', ''))):
            module.lab_window(bootstrap, config)
            for target, name, kwargs in (
                (module.sys, 'platform', {'new': 'darwin'}),
                (module.os, 'geteuid', {'return_value': 501}),
                (module.Path, 'read_text', {'return_value': 'production-host\n'}),
                (module, 'run', {'return_value': subprocess.CompletedProcess([], 0, 'kvm\n', '')}),
            ):
                with self.subTest(check=name), patch.object(target, name, **kwargs):
                    with self.assertRaises((AssertionError, SystemExit)):
                        module.lab_window(bootstrap, config)

    def test_cli_blocked_record_does_not_claim_its_private_rejection_gate(self):
        module = load('bootstrap_daytime_evidence', 'bootstrap_exercise.py')
        self.assertTrue(hasattr(module, 'cli_rejection_evidence'), 'separate CLI evidence missing')
        values = module.cli_rejection_evidence()
        self.assertEqual(values['cli_rejection_gate'], 'not_exposed')
        self.assertTrue(values['production_window_unchanged'])
        self.assertIn('actual_santiago', values)
        self.assertIsInstance(values['production_window_open_at_observation'], bool)


class SyntheticBodyTests(unittest.TestCase):
    def test_legacy_body_sends_real_ping_payload_and_keeps_only_two_ground_truth_records(self):
        module = load('bootstrap_ping_body', 'bootstrap_exercise.py')
        self.assertTrue(hasattr(module, 'legacy_source'), 'synthetic ping body missing')
        unit = module.synthetic_units(HERE.parents[1])['estrado-pjud-worker.service']
        writable = [Path(line.split('=', 1)[1]) for line in unit.splitlines() if line.startswith('ReadWritePaths=')]
        self.assertEqual(len(writable), 1)
        self.assertTrue(module.PING_FILE.is_relative_to(writable[0]),
                        'configured synthetic ping file is outside the real unit writable path')
        relative_ping = module.PING_FILE.relative_to(module.APP)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app = root / 'estrado-pjud-service'
            app.mkdir()
            path = app / relative_ping
            with (patch.object(module, 'REPO', root), patch.object(module, 'APP', app),
                  patch.object(module, 'PING_FILE', path),
                  patch.object(module.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid())),
                  patch.object(module.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=os.getgid()))):
                module.prepare_runtime_storage()
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.parent.stat().st_uid, os.getuid())
            self.assertEqual(path.parent.stat().st_gid, os.getgid())
            handlers, sent, connections = {}, [], []
            class SocketBoundary:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def connect(self, address): connections.append(address)
                def send(self, data):
                    sent.append(data)
                    return len(data)
            def pause(seconds):
                self.assertLessEqual(seconds, 1)
                if len(sent) >= 4:
                    handlers[module.signal.SIGTERM](module.signal.SIGTERM, None)
            namespace = {}
            with (patch.dict(os.environ, {'NOTIFY_SOCKET': '/synthetic-notify'}, clear=True),
                  patch.dict(sys.modules, {'worker.sd_notify': SimpleNamespace(notify_ready=lambda: None)}),
                  patch.object(socket, 'socket', return_value=SocketBoundary()),
                  patch.object(module.signal, 'signal', side_effect=lambda number, handler: handlers.update({number: handler})),
                  patch.object(time, 'sleep', side_effect=pause), redirect_stdout(io.StringIO())):
                exec(module.legacy_source(path), namespace)
            self.assertEqual(connections, ['/synthetic-notify'])
            self.assertEqual(sent, [b'WATCHDOG=1'] * 4)
            records = json.loads(path.read_text())
            self.assertEqual([row['sequence'] for row in records], [3, 4])
            self.assertTrue(all(row['pid'] == os.getpid() and row['sent_bytes'] == 10 for row in records))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])
            # Read-only Git rule evaluation; no init/index/commit and no source path excluded.
            gitdir = subprocess.run(['/usr/bin/git', '-C', str(HERE.parents[2]), 'rev-parse', '--absolute-git-dir'],
                capture_output=True, text=True, check=True, timeout=5).stdout.strip()
            ignored = subprocess.run(['/usr/bin/git', '-c', 'core.excludesFile=/dev/null',
                '--git-dir=' + gitdir, '--work-tree=' + str(root), 'check-ignore', '--no-index', '--stdin'],
                cwd=root, input='estrado-pjud-service/.venv/bin/python\n'
                    'estrado-pjud-service/logs/native-bootstrap-watchdog/latest.json\n'
                    'estrado-pjud-service/logs/native-bootstrap-watchdog/.latest.tmp\n'
                    'estrado-pjud-service/worker/__main__.py\nops/bootstrap-worker-maintenance.py\n'
                    'estrado-pjud-service/logs/unrelated.log\n',
                capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(ignored.stdout.splitlines(), ['estrado-pjud-service/.venv/bin/python',
                'estrado-pjud-service/logs/native-bootstrap-watchdog/latest.json',
                'estrado-pjud-service/logs/native-bootstrap-watchdog/.latest.tmp'])
            (app / 'logs/unrelated.log').write_text('unrelated evidence must stay visible')
            status = subprocess.run(['/usr/bin/git', '--no-optional-locks', '-c', 'core.excludesFile=/dev/null',
                '--git-dir=' + gitdir, '--work-tree=' + str(root), 'status', '--porcelain=v1',
                '--untracked-files=all', '--', 'estrado-pjud-service/logs/native-bootstrap-watchdog',
                'estrado-pjud-service/logs/unrelated.log'], cwd=root,
                capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(status.stdout, '?? estrado-pjud-service/logs/unrelated.log\n')

    def test_uvicorn_child_selection_excludes_wrapper_with_same_argv_path(self):
        module = load('bootstrap_identity_test', 'bootstrap_exercise.py')
        group = '/legaltech.slice/estrado-pjud.service'
        wrapper = dict(pid=11, parent_pid=1, uid=991, cgroup='0::' + group)
        child = dict(pid=13, parent_pid=11, uid=991, cgroup='0::' + group)
        uvicorn = str(module.APP / '.venv/bin/uvicorn').encode()
        commands = {11: [b'/bin/sh', b'/usr/bin/xvfb-run', b'-a', uvicorn, b'app.main:app'],
                    12: [b'/usr/bin/Xvfb'], 13: [b'python', uvicorn, b'app.main:app']}
        # Only external kernel/systemd/user-database boundaries are doubled on
        # this non-Linux host; the actual selection and identity checks run.
        with (patch.object(module, 'properties', return_value=dict(
                ActiveState='active', SubState='running', MainPID='11', ControlGroup=group, InvocationID='synthetic')),
             patch.object(module, 'group_pids', return_value=[11, 12, 13]),
             patch.object(module, 'commandline', side_effect=commands.__getitem__),
             patch.object(module, 'identity', side_effect={11: wrapper, 13: child}.__getitem__),
             patch.object(module.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=991))):
            observed = module.api_identity()
        self.assertEqual(observed['child']['pid'], 13)
        self.assertEqual(observed['wrapper']['pid'], 11)

    def test_guest_entrypoint_refuses_host_without_mutation(self):
        if sys.platform == 'linux' and os.geteuid() == 0:
            self.skipTest('host refusal characterized on non-Linux/root environment')
        result = subprocess.run([sys.executable, '-B', str(HERE / 'bootstrap_exercise.py')],
                                env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Requires isolated Linux/root QEMU guest', result.stderr)
        self.assertNotIn('DIAGNOSTIC', result.stdout)

    def test_synthetic_units_only_remove_environment_file_and_worker_runtime(self):
        module = load('bootstrap_units_test', 'bootstrap_exercise.py')
        self.assertTrue(hasattr(module, 'synthetic_units'), 'legacy unit renderer missing')
        units = module.synthetic_units(HERE.parents[1])
        for name in ('estrado-pjud.service', 'estrado-pjud-worker.service'):
            original = (HERE.parents[1] / 'systemd' / name).read_text()
            expected = original.replace('EnvironmentFile=/opt/legal-tech-microservices/estrado-pjud-service/.env\n', '')
            if 'worker' in name:
                expected = expected.replace('RuntimeDirectory=worker-maintenance\nRuntimeDirectoryMode=0700\n', '')
            self.assertEqual(units[name], expected)

    def test_equal_exit_status_does_not_classify_failed_lifespan_as_complete(self):
        module = load('bootstrap_outcome_test', 'bootstrap_exercise.py')
        self.assertTrue(hasattr(module, 'shutdown_evidence'), 'finite shutdown classifier missing')
        values = dict(ExecMainCode='1', ExecMainStatus='143', Result='exit-code', MainPID='0',
                      ActiveState='failed', SubState='failed')
        clean = 'NATIVE_SYNTHETIC_SHUTDOWN_COMPLETE\nApplication shutdown complete.'
        error = ("NATIVE_SYNTHETIC_CLEANUP_FAILED\nASGI 'lifespan' protocol appears unsupported.\n"
                 'Application shutdown complete.')
        self.assertTrue(module.shutdown_evidence('normal', values, clean)['lifespan_completed'])
        failed = module.shutdown_evidence('lifespan_error', values, error)
        self.assertFalse(failed['lifespan_completed'])
        self.assertTrue(failed['uvicorn_reported_complete'])
        with self.assertRaises(AssertionError):
            module.shutdown_evidence('normal', values, error)
        for changes in ({'ExecMainStatus': '0'}, {'ExecMainCode': '2'}, {'MainPID': '55'}, {'Result': 'success'}):
            with self.subTest(changes=changes), self.assertRaises(AssertionError):
                module.shutdown_evidence('normal', values | changes, clean)

    def test_real_asgi_body_distinguishes_completed_and_failed_cleanup(self):
        self.assertTrue((HERE / 'bootstrap_exercise.py').is_file(), 'native characterization body missing')
        module = load('bootstrap_exercise_test', 'bootstrap_exercise.py')
        with tempfile.TemporaryDirectory() as directory:
            mode = Path(directory) / 'case'
            namespace = {}
            exec(module.asgi_source(mode), namespace)
            for case in ('normal', 'lifespan_error'):
                mode.write_text(case)
                events = iter(({'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}))
                messages = []
                async def receive():
                    return next(events)
                async def send(message):
                    messages.append(message)
                call = namespace['app']({'type': 'lifespan'}, receive, send)
                if case == 'normal':
                    asyncio.run(call)
                    self.assertEqual(messages, [{'type': 'lifespan.startup.complete'},
                                                {'type': 'lifespan.shutdown.complete'}])
                else:
                    with self.assertRaisesRegex(RuntimeError, 'synthetic lifespan cleanup failure'):
                        asyncio.run(call)
                    self.assertEqual(messages, [{'type': 'lifespan.startup.complete'}])


class SnapshotFailureOriginTests(unittest.TestCase):
    """Actual frozen snapshot/function traceback; only systemctl is a boundary double."""
    def setUp(self):
        self.harness = load('bootstrap_origin_test', 'bootstrap_exercise.py')
        self.bootstrap = self.harness.load_bootstrap(HERE.parents[1] / 'bootstrap-worker-maintenance.py')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        systemd, proc, cgroups = root / 'systemd', root / 'proc', root / 'cgroups'
        systemd.mkdir(); cgroups.mkdir()
        (proc / 'sys/kernel/random').mkdir(parents=True)
        self.boot_file = proc / 'sys/kernel/random/boot_id'
        self.boot_file.write_text('aaaaaaaa-2222-4333-8444-555555555555')
        self.config = self.bootstrap.Config('a' * 40, systemd_dir=systemd, proc_root=proc,
            cgroup_root=cgroups, root_uid=os.getuid(), root_gid=os.getgid())
        self.services = {}
        for number, unit in enumerate(self.bootstrap.UNITS, 101):
            path = systemd / unit
            path.write_text('[Service]\n')
            path.chmod(0o644)
            os.chown(path, os.getuid(), os.getgid())
            self.services[unit] = dict(LoadState='loaded', FragmentPath=str(path), DropInPaths='',
                NeedDaemonReload='no', UnitFileState='disabled', ActiveState='inactive', SubState='dead',
                Result='success', MainPID='0', ExecMainPID=str(number), ExecMainCode='1',
                ExecMainStatus='0', ExecMainExitTimestampMonotonic='12345', ControlGroup='',
                Slice='legaltech.slice', Job='')

    def runner(self, command, **kwargs):
        self.assertEqual(command[:2], ['/usr/bin/systemctl', 'show'])
        values = self.services[command[2]]
        return subprocess.CompletedProcess(command, 0,
            ''.join(f'{key}={values[key]}\n' for key in self.bootstrap.PROPERTIES).encode(), b'')

    def snapshot_error(self):
        try:
            self.bootstrap.stopped_snapshot(self.config, self.runner)
        except self.bootstrap.MaintenanceError as error:
            return error
        self.fail('fixture did not reach a real MaintenanceError')

    def verify(self, error, module=None):
        self.assertTrue(hasattr(self.harness, 'verify_snapshot_state_rejection'),
                        'exact snapshot failure observer missing')
        return self.harness.verify_snapshot_state_rejection(module or self.bootstrap, error)

    def test_real_active_and_143_predicate_rejections_are_identified(self):
        api = self.services[self.bootstrap.UNITS[0]]
        for changes in ({'ActiveState': 'active'}, {'ExecMainStatus': '143'}):
            with self.subTest(changes=changes):
                original = dict(api)
                api.update(changes)
                self.assertIsNone(self.verify(self.snapshot_error()))
                api.clear(); api.update(original)

    def test_real_dropin_metadata_boot_and_cgroup_errors_are_not_state_rejection(self):
        api = self.services[self.bootstrap.UNITS[0]]
        api['DropInPaths'] = '/unrelated/dropin.conf'
        dropin_error = self.snapshot_error()
        api['DropInPaths'] = ''
        unit = self.config.systemd_dir / self.bootstrap.UNITS[0]
        unit.chmod(0o600)
        metadata_error = self.snapshot_error()
        unit.chmod(0o644)
        self.boot_file.write_text('AAAAAAAA-2222-4333-8444-555555555555')
        boot_error = self.snapshot_error()
        self.boot_file.write_text('aaaaaaaa-2222-4333-8444-555555555555')
        group = self.config.cgroup_root / 'legaltech.slice' / self.bootstrap.UNITS[0]
        group.mkdir(parents=True)
        (group / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
        cgroup_error = self.snapshot_error()
        for reason, error in (('dropin', dropin_error), ('metadata', metadata_error),
                              ('boot', boot_error), ('cgroup', cgroup_error)):
            with self.subTest(reason=reason), self.assertRaises(AssertionError):
                self.verify(error)

    def test_source_drift_or_replaced_function_does_not_authenticate_old_traceback(self):
        self.services[self.bootstrap.UNITS[0]]['ExecMainStatus'] = '143'
        error = self.snapshot_error()
        drifted = Path(self.temporary.name) / 'changed-installer.py'
        drifted.write_bytes(Path(self.bootstrap.__file__).read_bytes() + b'\n# drift\n')
        for module in (
            SimpleNamespace(__file__=str(drifted), stopped_snapshot=self.bootstrap.stopped_snapshot,
                            require=self.bootstrap.require),
            SimpleNamespace(__file__=self.bootstrap.__file__, stopped_snapshot=lambda *args: None,
                            require=self.bootstrap.require),
        ):
            with self.subTest(module=module), self.assertRaises(AssertionError):
                self.verify(error, module)


class FinalExecutionMetadataTests(unittest.TestCase):
    runner = SnapshotFailureOriginTests.runner
    snapshot_error = SnapshotFailureOriginTests.snapshot_error
    verify = SnapshotFailureOriginTests.verify

    def setUp(self):
        SnapshotFailureOriginTests.setUp(self)
        root = Path(self.temporary.name).resolve()
        self.config.global_lock = root / 'global.lock'
        self.config.global_lock.write_text('')
        self.config.global_lock.chmod(0o600)
        os.chown(self.config.global_lock, os.getuid(), os.getgid())
        for key in ('control_dir', 'ack_dir', 'journal_root', 'bootstrap_root'):
            setattr(self.config, key, root / key)
        self.aux = {}
        self.priors = {}
        for number, unit in enumerate(self.bootstrap.UNITS, 101):
            invocation = ('a' if number == 101 else 'b') * 32
            group = '/legaltech.slice/' + unit
            members = [dict(pid=number + offset, parent_pid=1, start_ticks=500 + offset,
                boot_id=self.boot_file.read_text(), uid=self.config.worker_uid, cgroup='0::' + group)
                for offset in (0, 10, 20)]
            self.aux[unit] = dict(self.services[unit], ControlPID='0', NRestarts='0',
                InvocationID=invocation, ExecMainStartTimestampMonotonic='100',
                ActiveEnterTimestampMonotonic='101', StateChangeTimestampMonotonic='12346')
            self.priors[unit] = dict(unit=unit, members=members, invocation=invocation,
                exec_main_pid=str(number), exec_main_start='100',
                files=None)
        self.services[self.bootstrap.UNITS[0]].update(ActiveState='failed', SubState='failed',
            Result='exit-code', ExecMainStatus='143')
        self.sync_aux()

    def sync_aux(self):
        for unit in self.bootstrap.UNITS:
            self.aux[unit].update(self.services[unit])

    def unavailable(self):
        unit = self.bootstrap.UNITS[1]
        self.services[unit].update(ExecMainCode='0', ExecMainStatus='0', ExecMainPID='0',
                                  ExecMainExitTimestampMonotonic='0')
        self.aux[unit].update(InvocationID='', ExecMainStartTimestampMonotonic='0',
            ActiveEnterTimestampMonotonic='0', StateChangeTimestampMonotonic='0')
        self.sync_aux()

    def validate(self):
        return self.harness.final_execution_metadata(self.bootstrap, self.config,
            self.services, self.aux, self.priors)

    def test_both_exact_worker_forms_and_real_api_frame(self):
        for unavailable in (False, True):
            if unavailable:
                self.unavailable()
            with self.subTest(unavailable=unavailable):
                result = self.validate()
                self.assertEqual(result, dict(worker_exit_metadata='execution-metadata-unavailable'
                    if unavailable else 'retained-clean-exit-record',
                    worker_exit_status='unknown' if unavailable else '0'))
                self.harness.verify_snapshot_state_rejection(self.bootstrap, self.snapshot_error(),
                    stopped=(self.config, self.services, self.aux, self.priors))
                if unavailable:
                    self.assertNotRegex(json.dumps(result), r'clean|exit0|succeeded|bootstrap complete')

    def test_worker_iteration_at_same_real_line_is_not_api_evidence(self):
        self.unavailable()
        self.services[self.bootstrap.UNITS[0]].update(ActiveState='inactive', SubState='dead',
                                                     Result='success', ExecMainStatus='0')
        self.sync_aux()
        error = self.snapshot_error()
        self.assertIsNone(self.verify(error))  # Same authenticated line, wrong iteration.
        with self.assertRaises(AssertionError):
            self.harness.verify_snapshot_state_rejection(self.bootstrap, error,
                stopped=(self.config, self.services, self.aux, self.priors))

    def test_partial_unknown_or_inconsistent_metadata_rejects(self):
        self.unavailable()
        worker, api = self.bootstrap.UNITS[1], self.bootstrap.UNITS[0]
        mutations = [(worker, key, value) for key, value in {
            'ExecMainPID': '102', 'ExecMainCode': '1', 'ExecMainStatus': '9',
            'ExecMainStartTimestampMonotonic': '100', 'ExecMainExitTimestampMonotonic': '1',
            'ActiveEnterTimestampMonotonic': '1', 'StateChangeTimestampMonotonic': '1',
            'InvocationID': 'b' * 32, 'ControlPID': '1', 'NRestarts': '1', 'Job': '42',
            'ControlGroup': '/legaltech.slice/' + worker, 'Result': 'exit-code',
            'ActiveState': 'failed', 'SubState': 'failed', 'MainPID': '102',
        }.items()] + [(api, key, value) for key, value in {
            'ExecMainStatus': '0', 'ExecMainPID': '0', 'ExecMainStartTimestampMonotonic': '0',
            'ExecMainExitTimestampMonotonic': '0', 'InvocationID': 'c' * 32,
            'ControlGroup': '/legaltech.slice/' + api,
        }.items()]
        for unit, key, value in mutations:
            with self.subTest(unit=unit, key=key):
                original = self.aux[unit][key]
                self.aux[unit][key] = value
                if key in self.services[unit]:
                    self.services[unit][key] = value
                with self.assertRaises(AssertionError):
                    self.validate()
                self.aux[unit][key] = original
                if key in self.services[unit]:
                    self.services[unit][key] = original
        self.aux[worker]['ExecMainStatus'] = '9'  # Auxiliary/raw mismatch is never merged.
        with self.assertRaises(AssertionError):
            self.validate()

    def test_retained_record_must_match_prior_execution(self):
        worker = self.bootstrap.UNITS[1]
        for key, value in {'ExecMainPID': '999', 'ExecMainStartTimestampMonotonic': '99',
                           'InvocationID': 'c' * 32, 'ActiveEnterTimestampMonotonic': '0',
                           'StateChangeTimestampMonotonic': '0', 'ExecMainExitTimestampMonotonic': '99'}.items():
            with self.subTest(key=key):
                old = self.aux[worker][key]
                self.aux[worker][key] = value
                if key in self.services[worker]:
                    self.services[worker][key] = value
                with self.assertRaises(AssertionError):
                    self.validate()
                self.aux[worker][key] = old
                if key in self.services[worker]:
                    self.services[worker][key] = old

    def test_every_prior_pid_absence_boot_and_empty_groups_are_required(self):
        self.unavailable()
        for prior in self.priors.values():
            for member in prior['members']:
                path = self.config.proc_root / str(member['pid'])
                path.mkdir()
                with self.subTest(pid=member['pid']), self.assertRaises(self.bootstrap.MaintenanceError):
                    self.validate()
                path.rmdir()
        self.boot_file.write_text('cccccccc-2222-4333-8444-555555555555')
        with self.assertRaises(AssertionError):
            self.validate()
        self.boot_file.write_text(self.priors[self.bootstrap.UNITS[1]]['members'][0]['boot_id'])
        group = self.config.cgroup_root / 'legaltech.slice' / self.bootstrap.UNITS[1]
        group.mkdir(parents=True)
        (group / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
        with self.assertRaises(self.bootstrap.MaintenanceError):
            self.validate()

    def prepare_proof_files(self):
        for unit, body in self.harness.synthetic_units(HERE.parents[1]).items():
            (self.config.systemd_dir / unit).write_text(body)
        drop = self.config.systemd_dir / (self.bootstrap.UNITS[1] + '.d/xvfb.conf')
        drop.parent.mkdir()
        drop.write_text((HERE.parents[1] / 'systemd/estrado-pjud-worker.service.d/xvfb.conf').read_text())
        drop.chmod(0o644)
        os.chown(drop, os.getuid(), os.getgid())
        self.services[self.bootstrap.UNITS[1]]['DropInPaths'] = str(drop)
        self.sync_aux()
        self.config.repo_dir = Path(self.temporary.name).resolve() / 'repo'
        gitdir = self.config.repo_dir / '.git'
        gitdir.mkdir(parents=True)
        # Owned temporary metadata only; never open or modify the worktree index.
        for name in ('HEAD', 'index', 'config'):
            (gitdir / name).write_text('synthetic boundary metadata\n')
        for prior in self.priors.values():
            prior['files'] = self.harness.execution_files(self.bootstrap, self.config)

    def proof_runner(self, command, **kwargs):
        command = list(command)
        self.commands.append(command)
        if command[:2] == ['/usr/bin/systemctl', 'show']:
            values = self.aux[command[2]]
            keys = [arg.removeprefix('--property=') for arg in command[3:]]
            output = ''.join(f'{key}={values[key]}\n' for key in keys)
        elif command[0] == '/usr/bin/git':
            last = command[-1]
            output = (str(self.config.repo_dir) if last == '--show-toplevel' else
                str(self.config.repo_dir / '.git') if last in ('--absolute-git-dir', '--git-common-dir') else
                self.config.expected_sha if last == 'HEAD' else '')
        else:
            self.assertEqual(command[:2], ['/usr/bin/python3', '-B'])
            self.assertEqual(command[-3:], ['install', '--expected-sha', self.config.expected_sha])
            if self.cli_mutation:
                self.cli_mutation()
            output = json.dumps(dict(operation_id=None, phase='validation', result='blocked'))
            return subprocess.CompletedProcess(command, 1, output, '')
        return subprocess.CompletedProcess(command, 0, output if kwargs.get('text') else output.encode(), '')

    def prove(self):
        self.commands, self.cli_mutation = [], getattr(self, 'cli_mutation', None)
        output = io.StringIO()
        with (patch.object(self.harness.subprocess, 'run', side_effect=self.proof_runner),
              patch.object(self.harness, 'lab_window'), redirect_stdout(output)):
            self.harness.prove_rejection(self.bootstrap, self.config, active=False, priors=self.priors)
        return output.getvalue()

    def test_actual_proof_keeps_real_state_files_locks_and_snapshot_unchanged(self):
        self.prepare_proof_files()
        for unavailable in (False, True):
            if unavailable:
                self.unavailable()
            before = self.harness.execution_files(self.bootstrap, self.config)
            with self.subTest(unavailable=unavailable):
                text = self.prove()
                self.assertIn('143 SNAPSHOT', text)
                self.assertIn('CLI blocked separately, gate not exposed', text)
                self.assertEqual(before, self.harness.execution_files(self.bootstrap, self.config))
                for path in (self.config.control_dir, self.config.ack_dir,
                             self.config.journal_root, self.config.bootstrap_root):
                    self.assertFalse(path.exists())
                if unavailable:
                    record = next(line for line in text.splitlines() if line.startswith('NATIVE WORKER EXECUTION METADATA'))
                    self.assertIn('unknown', record)
                    self.assertNotRegex(record, r'clean|exit0|succeeded|bootstrap complete')

    def test_byte_or_lock_drift_prevents_snapshot_and_cli_auxiliary_drift_aborts(self):
        self.prepare_proof_files()
        self.unavailable()
        unit = self.config.systemd_dir / self.bootstrap.UNITS[0]
        original = unit.read_text()
        unit.write_text(original + '\n# drift\n')
        with self.assertRaises(AssertionError):
            self.prove()
        self.assertEqual(sum(command[:2] == ['/usr/bin/systemctl', 'show'] for command in self.commands), 2)
        unit.write_text(original)
        lock = self.config.global_lock
        retained = lock.with_name('retained.lock')
        lock.rename(retained)
        lock.write_text(''); lock.chmod(0o600)
        os.chown(lock, os.getuid(), os.getgid())
        with self.assertRaises(AssertionError):
            self.prove()
        lock.unlink(); retained.rename(lock)
        self.cli_mutation = lambda: self.aux[self.bootstrap.UNITS[1]].update(NRestarts='1')
        with self.assertRaises(AssertionError):
            self.prove()

    def test_capture_binds_actual_group_and_execution_pid_for_api_and_worker(self):
        self.prepare_proof_files()
        for unit in self.bootstrap.UNITS:
            prior = self.priors[unit]
            group = '/legaltech.slice/' + unit
            members = {member['pid']: dict(member) for member in prior['members']}
            if unit == self.bootstrap.UNITS[0]:
                for member in members.values():
                    member['uid'] = 33  # API www-data differs from estrado worker uid.
                observed = dict(wrapper=members[101], child=members[111],
                    group=group, invocation=prior['invocation'])
            else:
                observed = dict(worker=members[102], members=list(members.values()),
                    group=group, invocation=prior['invocation'])
            self.aux[unit].update(ActiveState='active', SubState='running', Result='success',
                MainPID=prior['exec_main_pid'], ControlGroup=group)
            self.commands, self.cli_mutation = [], None
            with (patch.object(self.harness.subprocess, 'run', side_effect=self.proof_runner),
                  patch.object(self.harness, 'group_pids', return_value=sorted(members)),
                  patch.object(self.harness, 'identity', side_effect=members.__getitem__)):
                captured = self.harness.execution_prior(self.bootstrap, self.config, unit, observed)
                self.assertEqual(captured['members'], list(members.values()))
                self.assertEqual(captured['exec_main_pid'], prior['exec_main_pid'])
                self.aux[unit]['ExecMainPID'] = '999'
                with self.assertRaises(AssertionError):
                    self.harness.execution_prior(self.bootstrap, self.config, unit, observed)

    def test_actual_characterize_keeps_worker_prior_for_both_api_cases(self):
        module, bootstrap = self.harness, self.bootstrap
        api, worker = bootstrap.UNITS
        api_members, worker_members = self.priors[api]['members'], self.priors[worker]['members']
        observed_api = dict(wrapper=api_members[0], child=api_members[1],
            group='/legaltech.slice/' + api, invocation=self.priors[api]['invocation'])
        observed_worker = dict(worker=worker_members[0], members=worker_members,
            group='/legaltech.slice/' + worker, invocation=self.priors[worker]['invocation'])
        proofs, captures, signals, startup_phases = [], [], [], []
        startup_pin = dict(ready=True)
        def startup(_bootstrap, _config):
            startup_phases.append('observed')
            return startup_pin
        def capture(_bootstrap, _config, unit, observed):
            captures.append(unit)
            return self.priors[unit] if unit == worker else dict(self.priors[unit])
        def prove(_bootstrap, _config, *, active, priors=None, startup=None):
            if active:
                self.assertIs(startup, startup_pin)
                startup_phases.append('proof')
            else:
                proofs.append(dict(priors))
        active = dict(UnitFileState='disabled', MainPID='102', ControlGroup=observed_worker['group'])
        with ExitStack() as stack:
            for context in (patch.object(module.Path, 'read_text', return_value='VERSION_ID="24.04"'),
              patch.object(module.Path, 'is_file', return_value=True),
              patch.object(module.os, 'pidfd_open', create=True),
              patch.object(module.signal, 'pidfd_send_signal', create=True),
              patch.object(module, 'load_bootstrap', return_value=bootstrap),
              patch.object(module, 'lab_window'), patch.object(module, 'setup', return_value=self.config),
              patch.object(module, 'MODE', Path(self.temporary.name) / 'mode'),
              patch.object(module, 'run', return_value=subprocess.CompletedProcess([], 0, 'synthetic journal', '')),
              patch.object(module, 'properties', return_value=active),
              patch.object(module, 'observe_worker_startup', side_effect=startup),
              patch.object(module, 'prove_rejection', side_effect=prove),
              patch.object(module, 'set_overrides', side_effect=[observed_worker, None]),
              patch.object(module, 'until', return_value={}),
              patch.object(module, 'api_identity', return_value=observed_api),
              patch.object(module, 'execution_prior', side_effect=capture),
              patch.object(module, 'watchdog_snapshot', return_value=observed_worker),
              patch.object(module, 'identity', return_value=worker_members[0]),
              patch.object(module, 'commandline', side_effect=lambda pid:
                  [b'python', b'-m', b'worker'] if pid == 102 else [b'/usr/bin/xvfb-run']),
              patch.object(module, 'signal_once', side_effect=lambda b, c, member: signals.append(member)),
              patch.object(module, 'shutdown_evidence', return_value={}),
              patch.object(bootstrap, 'empty_cgroups'), patch.object(module, 'remove_overrides'),
              redirect_stdout(io.StringIO())):
                stack.enter_context(context)
            module.characterize()
        self.assertEqual(captures, [api, worker, worker, api])
        self.assertEqual(startup_phases, ['observed', 'proof'])
        self.assertEqual(len(proofs), 2)
        self.assertIs(proofs[0][worker], self.priors[worker])
        self.assertIs(proofs[1][worker], self.priors[worker])
        self.assertIsNot(proofs[0][api], proofs[1][api])
        self.assertEqual(signals, [observed_api['child'], observed_worker['worker'], observed_api['child']])


class StartupObservationTests(unittest.TestCase):
    sync_aux = FinalExecutionMetadataTests.sync_aux
    prepare_proof_files = FinalExecutionMetadataTests.prepare_proof_files
    proof_runner = FinalExecutionMetadataTests.proof_runner

    def setUp(self):
        FinalExecutionMetadataTests.setUp(self)
        self.assertTrue(hasattr(self.harness, 'observe_worker_startup'), 'post-handoff startup gate missing')
        self.prepare_proof_files()
        self.config.worker_uid, self.config.worker_gid = os.getuid(), os.getgid()
        self.unit = self.bootstrap.UNITS[1]
        self.group = '/legaltech.slice/' + self.unit
        self.proc, self.cgroups = self.config.proc_root, self.config.cgroup_root
        self.pings = Path(self.temporary.name).resolve() / 'pings/latest.json'
        self.pings.parent.mkdir(mode=0o700)
        self.pings.parent.chmod(0o700)
        os.chown(self.pings.parent, os.getuid(), os.getgid())
        self.clock, self.reads, self.commands = 0.0, 0, []
        self.transition, self.cli_mutation = None, None
        self.aux[self.unit].update(Type='notify', NotifyAccess='all', ActiveState='active',
            SubState='running', Result='success', MainPID='11', ExecMainPID='11',
            ExecMainCode='0', ExecMainStatus='0', ExecMainExitTimestampMonotonic='0',
            ControlGroup=self.group)
        self.aux[self.bootstrap.UNITS[0]].update(ActiveState='active', SubState='running', Result='success',
            MainPID='101', ExecMainCode='0', ExecMainStatus='0', ExecMainExitTimestampMonotonic='0')
        (self.cgroups / self.group.lstrip('/')).mkdir(parents=True)
        self.process(11, 1, [b'/bin/sh', b'/usr/bin/xvfb-run'])
        self.process(12, 11, [b'/usr/bin/Xvfb', b':99'])
        self.group_members([11, 12])

    def process(self, pid, parent, argv, ticks=None):
        path = self.proc / str(pid)
        path.mkdir(exist_ok=True)
        fields = ['S', str(parent), *(['0'] * 17), str(ticks or pid * 10)]
        (path / 'stat').write_text(f'{pid} (synthetic) ' + ' '.join(fields))
        (path / 'cgroup').write_text('0::' + self.group)
        (path / 'cmdline').write_bytes(b'\0'.join(argv) + b'\0')

    def group_members(self, pids):
        (self.cgroups / self.group.lstrip('/') / 'cgroup.procs').write_text('\n'.join(map(str, pids)))

    def publish(self, *, handoff=True):
        self.process(13, 11, [str(self.harness.APP / '.venv/bin/python').encode(), b'-m', b'worker'])
        self.group_members([11, 12, 13])
        rows = [dict(sequence=n, monotonic_ns=n * 100, pid=13, sent_bytes=10) for n in (1, 2)]
        self.pings.write_text(json.dumps(rows)); self.pings.chmod(0o600)
        os.chown(self.pings, os.getuid(), os.getgid())
        if handoff:
            self.aux[self.unit].update(MainPID='13', ExecMainPID='13', ExecMainStartTimestampMonotonic='130')

    def boundary(self, command, **kwargs):
        command = list(command)
        if command[:3] == ['/usr/bin/systemctl', 'show', self.unit]:
            self.reads += 1
            if self.reads == 2 and self.transition:
                self.transition()
        return self.proof_runner(command, **kwargs)

    def paths(self, *parts):
        value = Path(*parts)
        for old, new in ((Path('/proc'), self.proc), (Path('/sys/fs/cgroup'), self.cgroups)):
            if value.is_relative_to(old):
                return new / value.relative_to(old)
        return value

    def enter(self):
        stack = ExitStack()
        for context in (patch.object(self.harness, 'Path', side_effect=self.paths),
                patch.object(self.harness, 'PING_FILE', self.pings),
                patch.object(self.harness.subprocess, 'run', side_effect=self.boundary),
                patch.object(self.harness.time, 'monotonic', side_effect=lambda: self.clock),
                patch.object(self.harness.time, 'sleep', side_effect=self.advance),
                patch.object(self.harness, 'lab_window'), redirect_stdout(io.StringIO())):
            stack.enter_context(context)
        return stack

    def advance(self, seconds):
        self.assertLessEqual(seconds, .05)
        self.clock += seconds

    def observe(self):
        with self.enter():
            return self.harness.observe_worker_startup(self.bootstrap, self.config)

    def test_wrapper_ready_is_pending_until_real_worker_pid_and_two_pings(self):
        self.transition = self.publish
        observed = self.observe()
        self.assertEqual(observed['values']['MainPID'], '13')
        self.assertEqual({member['pid'] for member in observed['members']}, {11, 12, 13})
        self.assertGreaterEqual(self.reads, 2)
        self.assertFalse(any(command[0] == '/usr/bin/python3' for command in self.commands))

    def test_no_handoff_even_with_extra_ready_and_pings_expires_without_cli(self):
        self.publish(handoff=False)
        with self.assertRaisesRegex(AssertionError, 'timed out'):
            self.observe()
        self.assertLessEqual(self.clock, 30)
        self.assertFalse(any(command[0] == '/usr/bin/python3' for command in self.commands))

    def test_wrong_main_failed_or_restart_is_never_pending(self):
        for key, value in {'MainPID': '12', 'ActiveState': 'failed', 'NRestarts': '1',
                           'Job': '42', 'ControlPID': '9', 'Result': 'exit-code'}.items():
            with self.subTest(key=key):
                previous = self.aux[self.unit][key]
                self.aux[self.unit][key] = value
                with self.assertRaises(AssertionError):
                    self.observe()
                self.aux[self.unit][key] = previous
                self.assertEqual(self.clock, 0)

    def test_boot_invocation_or_wrapper_identity_change_before_handoff_aborts(self):
        def drift():
            self.publish()
            if self.fault == 'boot':
                self.boot_file.write_text('cccccccc-2222-4333-8444-555555555555')
            elif self.fault == 'invocation':
                self.aux[self.unit]['InvocationID'] = 'c' * 32
            else:
                self.process(11, 1, [b'/bin/sh', b'/usr/bin/xvfb-run'], ticks=999)
        for fault in ('boot', 'invocation', 'wrapper'):
            with self.subTest(fault=fault):
                self.fault, self.transition, self.reads = fault, drift, 0
                self.boot_file.write_text('aaaaaaaa-2222-4333-8444-555555555555')
                self.aux[self.unit].update(InvocationID='b' * 32, MainPID='11')
                self.process(11, 1, [b'/bin/sh', b'/usr/bin/xvfb-run'])
                with self.assertRaises(AssertionError):
                    self.observe()

    def test_existing_untrusted_ping_file_never_becomes_pending(self):
        self.publish(handoff=False)
        self.pings.chmod(0o644)
        with self.assertRaises(AssertionError):
            self.observe()
        self.assertEqual(self.clock, 0)

    def test_non_python_argv_and_untrusted_unit_are_not_ready(self):
        self.publish()
        self.process(13, 11, [b'/usr/bin/not-python', b'-m', b'worker'])
        with self.assertRaises(AssertionError):
            self.observe()
        self.publish()
        (self.config.systemd_dir / self.unit).chmod(0o600)
        with self.assertRaises(self.bootstrap.MaintenanceError):
            self.observe()
        self.assertEqual(self.clock, 0)

    def test_post_pin_identity_drift_aborts_before_cli(self):
        self.publish()
        pinned = self.observe()
        self.process(13, 11, [str(self.harness.APP / '.venv/bin/python').encode(), b'-m', b'worker'], ticks=999)
        with self.enter(), self.assertRaises(AssertionError):
            self.harness.prove_rejection(self.bootstrap, self.config, active=True, startup=pinned)
        self.assertFalse(any(command[0] == '/usr/bin/python3' for command in self.commands))

    def test_real_active_proof_runs_cli_once_and_prints_finite_mismatch(self):
        self.publish()
        pinned = self.observe()
        self.cli_mutation = lambda: self.aux[self.unit].update(MainPID='11')
        output = io.StringIO()
        with self.enter(), redirect_stdout(output), self.assertRaises(AssertionError):
            self.harness.prove_rejection(self.bootstrap, self.config, active=True, startup=pinned)
        self.assertEqual(sum(command[0] == '/usr/bin/python3' for command in self.commands), 1)
        records = [json.loads(line.removeprefix('NATIVE INSTALLED SNAPSHOT MISMATCH '))
            for line in output.getvalue().splitlines() if line.startswith('NATIVE INSTALLED SNAPSHOT MISMATCH ')]
        self.assertEqual(records, [dict(phase='active-legacy', unit=self.unit,
            differing_keys=['MainPID'], before={'MainPID': '13'}, after={'MainPID': '11'})])
        self.assertNotIn('Environment', output.getvalue())

    def test_real_active_proof_accepts_stable_pin_and_calls_cli_only_once(self):
        self.publish()
        pinned = self.observe()
        output = io.StringIO()
        with self.enter(), redirect_stdout(output):
            self.harness.prove_rejection(self.bootstrap, self.config, active=True, startup=pinned)
        self.assertEqual(sum(command[0] == '/usr/bin/python3' for command in self.commands), 1)
        self.assertIn('NATIVE INSTALLER REJECTED REAL ACTIVE LEGACY', output.getvalue())


class AbortDiagnosticsTests(unittest.TestCase):
    def test_main_non_qemu_refusal_never_collects_diagnostics(self):
        module = load('bootstrap_unverified_guest', 'bootstrap_exercise.py')
        calls, output = [], io.StringIO()
        def command(*args, **kwargs):
            calls.append(args)
            self.assertEqual(args, ('/usr/bin/systemd-detect-virt',))
            return subprocess.CompletedProcess(args, 0, 'kvm\n', '')
        with (patch.object(module.sys, 'platform', 'linux'),
              patch.object(module.os, 'geteuid', return_value=0),
              patch.object(module.Path, 'read_text', return_value='native-guards\n'),
              patch.object(module, 'run', side_effect=command), redirect_stdout(output)):
            with self.assertRaises(AssertionError):
                module.main(allow_daytime_lab=True)
        self.assertEqual(calls, [('/usr/bin/systemd-detect-virt',)])
        self.assertNotIn('DIAGNOSTIC', output.getvalue())

    def test_real_property_parser_and_finite_journal_capture_without_pid_attribution(self):
        module = load('bootstrap_abort_diagnostics', 'bootstrap_exercise.py')
        self.assertTrue(hasattr(module, 'worker_abort_diagnostics'), 'bounded worker diagnostics missing')
        output, calls = io.StringIO(), []
        values = dict(InvocationID='', ExecMainCode='1', ExecMainStatus='1', NRestarts='0',
            ExecMainStartTimestampMonotonic='123', ExecMainExitTimestampMonotonic='456',
            ActiveEnterTimestampMonotonic='234', StateChangeTimestampMonotonic='567',
            ProtectSystem='strict', ReadWritePaths='/synthetic/logs', MainPID='0',
            ActiveState='failed', SubState='failed', Result='exit-code', ControlPID='0', Job='')
        def command(*args, **kwargs):
            calls.append((args, kwargs))
            if args == ('/usr/bin/systemd-detect-virt',):
                return subprocess.CompletedProcess(args, 0, 'qemu\n', '')
            self.assertLessEqual(kwargs['timeout'], 5)
            if args[:3] == ('/usr/bin/systemctl', 'show', 'estrado-pjud-worker.service'):
                keys = [item.removeprefix('--property=') for item in args[3:]]
                self.assertEqual(set(keys), set(values))
                return subprocess.CompletedProcess(args, 0, ''.join(f'{k}={values[k]}\n' for k in keys), '')
            if args[0] == '/usr/bin/journalctl':
                self.assertEqual(args, ('/usr/bin/journalctl', '--no-pager', '-b', '-u',
                    'estrado-pjud-worker.service', '-n', '40', '-o', 'short-monotonic'))
                return subprocess.CompletedProcess(args, 0, 'x' * 20000 + '\nOSError: synthetic failure', '')
            self.assertEqual(args[:5], ('/usr/bin/findmnt', '--noheadings', '--output', 'TARGET,FSTYPE,OPTIONS', '--target'))
            return subprocess.CompletedProcess(args, 0, '/ ext4 rw\n', '')
        with tempfile.TemporaryDirectory() as directory:
            ping_path = Path(directory).resolve() / 'latest.json'
            with (patch.object(module.sys, 'platform', 'linux'),
                  patch.object(module.os, 'geteuid', return_value=0),
                  patch.object(module.Path, 'read_text', return_value='native-guards\n'),
                  patch.object(module, 'PING_FILE', ping_path), patch.object(module, 'run', side_effect=command),
                  redirect_stdout(output)):
                module.worker_abort_diagnostics('preconditions')
            lines = output.getvalue().splitlines()
            records = [json.loads(line.removeprefix('NATIVE WORKER DIAGNOSTIC ')) for line in lines]
            properties = next(row for row in records if row['kind'] == 'properties')
            self.assertEqual(properties['values'], values)
            journal = next(row for row in records if row['kind'] == 'journal')
            self.assertEqual(journal['scope'], 'exact-unit-current-boot-not-single-invocation')
            self.assertLessEqual(len(journal['text']), 16384)
            self.assertTrue(journal['text'].endswith('OSError: synthetic failure'))
            filesystem = next(row for row in records if row['kind'] == 'ping-directory')
            self.assertEqual(filesystem['metadata']['uid'], ping_path.parent.stat().st_uid)
            self.assertEqual(filesystem['scope'], 'guest-root-namespace-not-service')
            self.assertNotIn('Environment', output.getvalue())
            self.assertFalse(any('/proc/' in arg for args, _ in calls for arg in args))


class WatchdogAdministrationTests(unittest.TestCase):
    """Real orchestration/files; only manager, /proc and time-wait boundaries doubled."""
    def setUp(self):
        self.module = load('bootstrap_watchdog_test', 'bootstrap_exercise.py')
        self.bootstrap = self.module.load_bootstrap(HERE.parents[1] / 'bootstrap-worker-maintenance.py')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = self.bootstrap.Config('a' * 40, systemd_dir=self.root / 'systemd',
            root_uid=os.getuid(), root_gid=os.getgid(), worker_uid=os.getuid(), worker_gid=os.getgid())
        self.unit = 'estrado-pjud-worker.service'
        self.shutdown = self.root / 'runtime' / (self.unit + '.d') / '90-worker-bootstrap-shutdown.conf'
        self.helper = self.shutdown.with_name('91-native-watchdog-admin.conf')
        self.pings = self.root / 'pings' / 'latest.json'
        self.pings.parent.mkdir(mode=0o700)
        os.chown(self.pings.parent, os.getuid(), os.getgid())
        self.xvfb = self.config.systemd_dir / (self.unit + '.d/xvfb.conf')
        for path, body in ((self.shutdown, self.module.OVERRIDE),
                           (self.config.systemd_dir / self.unit,
                            self.module.synthetic_units(HERE.parents[1])[self.unit]),
                           (self.xvfb, (HERE.parents[1] / 'systemd' / (self.unit + '.d/xvfb.conf')).read_text())):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body); path.chmod(0o644)
            os.chown(path, os.getuid(), os.getgid())
        os.chown(self.shutdown.parent, os.getuid(), os.getgid())
        self.group = '/legaltech.slice/' + self.unit
        self.values = dict(Type='notify', NotifyAccess='all', ExecReload='', TimeoutStartUSec='1min 30s',
            ControlPID='0', PropagatesReloadTo='', ReloadPropagatedFrom='', TriggeredBy='',
            NeedDaemonReload='no', LoadState='loaded', ActiveState='active', SubState='running',
            Result='success', MainPID='13', ControlGroup=self.group,
            InvocationID='a' * 32, FragmentPath=str(self.config.systemd_dir / self.unit),
            DropInPaths=f'{self.xvfb} {self.shutdown}', UnitFileState='disabled', Restart='no',
            WatchdogUSec='5min', SendSIGKILL='no', TimeoutStopUSec='infinity', Job='')
        self.identities = {pid: dict(pid=pid, parent_pid=parent, start_ticks=pid * 10,
            boot_id='aaaaaaaa-2222-4333-8444-555555555555', uid=os.getuid(), cgroup='0::' + self.group)
            for pid, parent in ((11, 1), (12, 11), (13, 11))}
        self.commands = {11: [b'/bin/sh', b'/usr/bin/xvfb-run'], 12: [b'/usr/bin/Xvfb'],
                         13: [b'python', b'-m', b'worker']}
        self.calls, self.assignments = [], []
        self.reload_commands = []
        self.interval_usec = 300000000
        self.fail_after = None
        self.journal_failure = False
        self.journal_text = None
        self.rejection_failure = None
        self.guest_entry = False
        self.ping_mode = 'fresh'
        self.sequence = 0
        self.output = io.StringIO()

    def properties(self, unit, *args, **kwargs):
        if self.guest_entry and unit == self.module.UNITS[0]:
            return dict(UnitFileState='disabled', Restart='no', WatchdogUSec='0',
                        SendSIGKILL='no', TimeoutStopUSec='infinity', Job='')
        self.assertEqual(unit, self.unit)
        return dict(self.values)

    def manager(self, *args, **kwargs):
        self.calls.append(args)
        if self.guest_entry:
            entry_commands = {('/usr/bin/systemd-detect-virt',): 'qemu\n',
                ('/usr/bin/systemctl', '--version'): 'systemd 255 (host boundary)\n',
                ('/usr/bin/systemctl', 'start', *self.module.UNITS): ''}
            if args in entry_commands:
                return subprocess.CompletedProcess(args, 0, entry_commands[args], '')
        if args[0] == '/usr/bin/journalctl' and self.journal_failure:
            raise RuntimeError('diagnostic journal is unavailable')
        if args[0] == '/usr/bin/journalctl' and self.journal_text is not None:
            return subprocess.CompletedProcess(args, 0, self.journal_text, '')
        if args == ('/usr/bin/busctl', '--system', '--json=short', 'get-property',
                    'org.freedesktop.systemd1',
                    '/org/freedesktop/systemd1/unit/estrado_2dpjud_2dworker_2eservice',
                    'org.freedesktop.systemd1.Service', 'ExecReloadEx'):
            return subprocess.CompletedProcess(args, 0, json.dumps({
                'type': 'a(sasasttttuii)', 'data': self.reload_commands}), '')
        if args == ('/usr/bin/busctl', '--system', '--json=short', 'get-property',
                    'org.freedesktop.systemd1',
                    '/org/freedesktop/systemd1/unit/estrado_2dpjud_2dworker_2eservice',
                    'org.freedesktop.systemd1.Service', 'WatchdogUSec'):
            return subprocess.CompletedProcess(args, 0, json.dumps({'type': 't', 'data': self.interval_usec}), '')
        if args == ('/usr/bin/systemctl', 'daemon-reload'):
            if self.helper.exists():
                body = self.helper.read_text()
                self.assertIn(body, [
                    '[Service]\nTimeoutStartSec=infinity\nExecReload=/usr/bin/systemd-notify WATCHDOG_USEC=0\n',
                    '[Service]\nTimeoutStartSec=infinity\nExecReload=/usr/bin/systemd-notify WATCHDOG_USEC=300000000\n'])
                usec = body.split('WATCHDOG_USEC=')[1].strip()
                self.values['ExecReload'] = ('{ path=/usr/bin/systemd-notify ; argv[]=/usr/bin/systemd-notify '
                    f'WATCHDOG_USEC={usec} ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; '
                    'pid=0 ; code=(null) ; status=0/0 }')
                self.values['TimeoutStartUSec'] = 'infinity'
                self.reload_commands = [['/usr/bin/systemd-notify',
                    ['/usr/bin/systemd-notify', f'WATCHDOG_USEC={usec}'], [], 0, 0, 0, 0, 0, 0, 0]]
                self.values['DropInPaths'] = f'{self.xvfb} {self.shutdown} {self.helper}'
            else:
                self.values['ExecReload'] = ''
                self.reload_commands = []
                self.values['TimeoutStartUSec'] = '1min 30s'
                self.values['DropInPaths'] = f'{self.xvfb} {self.shutdown}'
            if self.fail_after == 'effective-command':
                self.reload_commands[0][2] = ['ignore-failure']
            elif self.fail_after == 'effective-timeout':
                self.values['TimeoutStartUSec'] = '1s'
        elif args == ('/usr/bin/systemctl', '--job-mode=fail', 'reload', self.unit):
            usec = self.helper.read_text().split('WATCHDOG_USEC=')[1].strip()
            self.assignments.append(usec)
            if self.fail_after == 'timeout':
                self.values['Job'] = '41'
                raise subprocess.TimeoutExpired(args, 30)
            if self.fail_after == 'command-failure':
                raise RuntimeError('synthetic manager refusal')
            self.values['WatchdogUSec'] = '0' if usec == '0' else '5min'
            self.interval_usec = int(usec)
            if isinstance(self.fail_after, dict):
                self.values.update(self.fail_after)
            elif self.fail_after == 'identity':
                self.identities[13] = self.identities[13] | {'start_ticks': 999}
            elif self.fail_after == 'foreign-helper':
                self.helper.write_text('foreign replacement')
        else:
            self.fail('unexpected host command: ' + repr(args))
        return subprocess.CompletedProcess(args, 0, '', '')

    def publish_pings(self):
        self.sequence += 2
        stamp = time.monotonic_ns() if self.ping_mode == 'fresh' else 1
        records = [dict(sequence=n, monotonic_ns=stamp, pid=13, sent_bytes=10)
                   for n in (self.sequence - 1, self.sequence)]
        self.pings.write_text(json.dumps(records)); self.pings.chmod(0o600)
        os.chown(self.pings, os.getuid(), os.getgid())

    def observe(self, predicate, label, seconds=30):
        for _ in range(3):
            self.publish_pings()
            value = predicate()
            if value:
                return value
        raise AssertionError('bounded observation failed: ' + label)

    def execute(self, main=False, allow_daytime_lab=False):
        self.assertTrue(hasattr(self.module, 'characterize_worker_watchdog'),
                        'contained administrative watchdog cycle missing')
        self.publish_pings()
        with ExitStack() as stack:
            for target, name, kwargs in (
                (self.module, 'REPO', {'new': HERE.parents[2]}),
                (self.module, 'PING_FILE', {'new': self.pings}),
                (self.module, 'override_path', {'side_effect': lambda unit: self.shutdown if unit == self.unit
                    else self.root / 'api-override.conf'}),
                (self.module, 'properties', {'side_effect': self.properties}),
                (self.module, 'run', {'side_effect': self.manager}),
                (self.module, 'identity', {'side_effect': lambda pid: dict(self.identities[pid])}),
                (self.module, 'group_pids', {'return_value': [11, 12, 13]}),
                (self.module, 'commandline', {'side_effect': self.commands.__getitem__}),
                (self.module, 'until', {'side_effect': self.observe}),
                (self.bootstrap, 'window', {}),
            ):
                stack.enter_context(patch.object(target, name, **kwargs))
            stack.enter_context(redirect_stdout(self.output))
            if not main:
                return self.module.characterize_worker_watchdog(self.bootstrap, self.config)
            self.guest_entry = True
            self.shutdown.unlink()  # main creates the actual shutdown overrides in this owned temp dir.
            original_read = Path.read_text
            def guest_text(path, *args, **kwargs):
                if str(path) == '/etc/hostname': return 'native-guards\n'
                if str(path) == '/etc/os-release': return 'VERSION_ID="24.04"\n'
                return original_read(path, *args, **kwargs)
            for target, name, kwargs in (
                (self.module.sys, 'platform', {'new': 'linux'}),
                (self.module.os, 'geteuid', {'return_value': 0}),
                (self.module.os, 'pidfd_open', {'create': True, 'side_effect': AssertionError('unexpected workload signal')}),
                (self.module.signal, 'pidfd_send_signal', {'create': True, 'side_effect': AssertionError('unexpected signal')}),
                (Path, 'read_text', {'new': guest_text}),
                (Path, 'is_file', {'return_value': True}),
                (self.module, 'load_bootstrap', {'return_value': self.bootstrap}),
                (self.module, 'setup', {'return_value': self.config}),
                (self.module, 'observe_worker_startup', {'return_value': {'ready': True}}),
                (self.module, 'prove_rejection', {'side_effect': self.rejection_failure}),
                (self.module.urllib.request, 'build_opener', {'side_effect': AssertionError('unexpected health request')}),
            ):
                stack.enter_context(patch.object(target, name, **kwargs))
            self.module.main(allow_daytime_lab=allow_daytime_lab)

    def test_zero_restore_zero_with_two_post_transition_pings_and_owned_removal(self):
        observed = self.execute()
        self.assertIsInstance(observed, dict, 'original identity must reach the later signal gates')
        self.assertEqual(observed['worker'], self.identities[13])
        self.assertEqual(self.assignments, ['0', '300000000', '0'])
        self.assertFalse(self.helper.exists())
        self.assertEqual(self.shutdown.read_text(), self.module.OVERRIDE)
        self.assertEqual(self.values['WatchdogUSec'], '0')
        records = [json.loads(line.removeprefix('NATIVE WATCHDOG PINGS '))
                   for line in self.output.getvalue().splitlines() if line.startswith('NATIVE WATCHDOG PINGS ')]
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]['pings']), 2)
        self.assertTrue(all(p['monotonic_ns'] > records[0]['after_zero_ns'] for p in records[0]['pings']))
        self.assertNotIn('argv[]', self.output.getvalue())

    def test_preconditions_block_before_helper_or_targeted_reload(self):
        for change in ({'Type': 'notify-reload'}, {'NotifyAccess': 'main'}, {'ExecReload': 'foreign'},
                       {'PropagatesReloadTo': 'other.service'}, {'ReloadPropagatedFrom': 'other.service'},
                       {'TriggeredBy': 'other.timer'}, {'ControlPID': '99'}, {'Job': '22'},
                       {'UnitFileState': 'enabled'}, {'Restart': 'always'}, {'WatchdogUSec': '0'},
                       {'SendSIGKILL': 'yes'}, {'TimeoutStopUSec': '90s'}, {'Result': 'watchdog'},
                       {'NeedDaemonReload': 'yes'}, {'DropInPaths': '/foreign.conf'}):
            original = dict(self.values)
            self.values.update(change)
            self.reload_commands = [['/foreign', ['/foreign'], [], 0, 0, 0, 0, 0, 0, 0]] if 'ExecReload' in change else []
            with self.subTest(change=change), self.assertRaises(AssertionError):
                self.execute()
            self.assertEqual(self.assignments, [])
            self.assertFalse(self.helper.exists())
            self.values = original

    def test_foreign_helper_or_shutdown_metadata_never_overwritten(self):
        self.helper.write_text('foreign')
        with self.assertRaises((AssertionError, self.bootstrap.MaintenanceError)):
            self.execute()
        self.assertEqual(self.helper.read_text(), 'foreign')
        self.assertFalse(any('reload' in command or 'daemon-reload' in command for command in self.calls))
        self.helper.unlink()
        self.shutdown.chmod(0o666)
        with self.assertRaises((AssertionError, self.bootstrap.MaintenanceError)):
            self.execute()
        self.assertFalse(any('reload' in command or 'daemon-reload' in command for command in self.calls))

    def test_bad_effective_helper_configuration_never_reloads_worker(self):
        self.fail_after = 'effective-command'
        with self.assertRaises(AssertionError):
            self.execute()
        self.assertEqual(self.assignments, [])

    def test_reload_failure_does_not_restore_retry_or_remove_evidence(self):
        self.fail_after = 'timeout'
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute(main=True)
        self.assertEqual(self.assignments, ['0'])
        self.assertTrue(self.helper.exists())
        self.assertIn('41', self.output.getvalue())

    def test_daytime_main_retains_watchdog_timeout_and_no_signal_guards(self):
        self.fail_after = 'timeout'
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute(main=True, allow_daytime_lab=True)
        self.assertEqual(self.assignments, ['0'])
        self.assertTrue(self.helper.exists())
        self.assertIn('"allow_daytime_lab": true', self.output.getvalue())

    def test_journal_collection_failure_preserves_original_timeout(self):
        self.fail_after = 'timeout'
        self.journal_failure = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute(main=True, allow_daytime_lab=True)
        self.assertTrue(any(args[0] == '/usr/bin/journalctl' for args in self.calls),
                        'failure path must attempt bounded journal collection')
        self.assertEqual(self.assignments, ['0'])
        self.assertTrue(self.helper.exists())

    def test_main_rejection_failure_before_watchdog_collects_journal_and_rethrows_same_error(self):
        expected = AssertionError('synthetic installed-files snapshot drift')
        self.rejection_failure = expected
        self.journal_text = 'synthetic worker traceback before watchdog'
        with self.assertRaises(AssertionError) as caught:
            self.execute(main=True, allow_daytime_lab=True)
        self.assertIs(caught.exception, expected)
        self.assertIn(self.journal_text, self.output.getvalue())
        self.assertIn('"phase": "characterization"', self.output.getvalue())
        self.assertEqual(sum(args[0] == '/usr/bin/journalctl' for args in self.calls), 1)
        self.assertEqual(self.assignments, [])
        self.assertFalse(self.helper.exists())

    def test_identity_drift_prevents_restore_and_workload_progress(self):
        self.fail_after = 'identity'
        with self.assertRaises(AssertionError):
            self.execute()
        self.assertEqual(self.assignments, ['0'])

    def test_post_reload_residual_control_process_blocks_restore(self):
        self.fail_after = {'ControlPID': '55'}
        with self.assertRaises(AssertionError):
            self.execute()
        self.assertEqual(self.assignments, ['0'])

    def test_stale_pings_do_not_prove_zero_coexistence(self):
        self.ping_mode = 'stale'
        with self.assertRaises(AssertionError):
            self.execute()
        self.assertEqual(self.assignments, ['0'])

    def test_command_failure_blocks_without_retry_or_cleanup(self):
        self.fail_after = 'command-failure'
        with self.assertRaisesRegex(RuntimeError, 'synthetic manager refusal'):
            self.execute()
        self.assertEqual(self.assignments, ['0'])
        self.assertTrue(self.helper.exists())

    def test_wrong_watchdog_after_reload_blocks_remaining_transitions(self):
        self.fail_after = {'WatchdogUSec': '5min'}
        with self.assertRaises(AssertionError):
            self.execute()
        self.assertEqual(self.assignments, ['0'])

    def test_residual_job_after_successful_client_return_blocks(self):
        self.fail_after = {'Job': '91'}
        with self.assertRaises(AssertionError):
            self.execute()
        self.assertEqual(self.assignments, ['0'])

    def test_finite_start_timeout_never_invokes_helper(self):
        self.fail_after = 'effective-timeout'
        with self.assertRaises(AssertionError):
            self.execute()
        self.assertEqual(self.assignments, [])

    def test_pretty_five_minutes_does_not_replace_exact_recorded_microseconds(self):
        self.interval_usec = 300000001
        with self.assertRaises(AssertionError):
            self.execute()
        self.assertEqual(self.assignments, [])
        self.assertFalse(self.helper.exists())

    def test_changed_owned_helper_is_not_replaced_or_removed(self):
        self.fail_after = 'foreign-helper'
        with self.assertRaises(AssertionError):
            self.execute()
        self.assertEqual(self.assignments, ['0'])
        self.assertEqual(self.helper.read_text(), 'foreign replacement')

    def test_structured_reload_argv_flags_and_types_fail_closed(self):
        good = ['/usr/bin/systemd-notify', ['/usr/bin/systemd-notify', 'WATCHDOG_USEC=0'],
                [], 0, 0, 0, 0, 0, 0, 0]
        malformed = [
            {'type': 'a(sasbttttuii)', 'data': [good]},
            {'type': 'a(sasasttttuii)', 'data': [good, good]},
            {'type': 'a(sasasttttuii)', 'data': [good[:1] + [['/usr/bin/systemd-notify WATCHDOG_USEC=0']] + good[2:]]},
            {'type': 'a(sasasttttuii)', 'data': [good[:2] + [['privileged']] + good[3:]]},
            {'type': 'a(sasasttttuii)', 'data': [good[:-1] + [False]]},
        ]
        for value in malformed:
            with self.subTest(value=value), patch.object(self.module, 'run',
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(value), '')):
                with self.assertRaises(AssertionError):
                    self.module.check_reload_command(0)


class OverrideDiagnosticsTests(unittest.TestCase):
    EXPECTED = {'UnitFileState': 'disabled', 'Restart': 'no', 'WatchdogUSec': '0',
                'SendSIGKILL': 'no', 'TimeoutStopUSec': 'infinity', 'Job': ''}

    def test_matching_override_logs_only_selected_properties_and_no_differences(self):
        module = load('bootstrap_override_success', 'bootstrap_exercise.py')
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(module, 'override_path', side_effect=lambda unit: Path(directory) / unit),
                  patch.object(module, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')),
                  patch.object(module, 'properties', return_value=self.EXPECTED | {'Environment': 'never-log-this'}),
                  redirect_stdout(output)):
                module.set_overrides(SimpleNamespace(window=lambda config: None), object(), module.UNITS[:1])
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, 'override diagnostics must precede each assertion')
        for unit, line in zip(module.UNITS, lines):
            prefix = 'NATIVE OVERRIDE CHECK '
            self.assertTrue(line.startswith(prefix))
            value = json.loads(line.removeprefix(prefix))
            self.assertEqual(value, {'phase': 'set_overrides', 'unit': unit,
                                     'properties': self.EXPECTED, 'differing_keys': []})
        self.assertNotIn('never-log-this', output.getvalue())

    def test_mismatch_in_real_main_logs_fields_then_aborts_before_any_signal(self):
        module = load('bootstrap_override_failure', 'bootstrap_exercise.py')
        output = io.StringIO()
        config = object()
        bootstrap = SimpleNamespace(Config=lambda sha: config, window=lambda config: None)
        observed = self.EXPECTED | {'Restart': 'always', 'WatchdogUSec': '5min',
                                    'Environment': 'never-log-this', 'ExecStart': 'not-selected'}
        def external_command(*args, **kwargs):
            outputs = {
                ('/usr/bin/systemd-detect-virt',): 'qemu\n',
                ('/usr/bin/systemctl', '--version'): 'systemd 255 (synthetic host fixture)\n',
                ('/usr/bin/systemctl', 'start', *module.UNITS): '',
                ('/usr/bin/systemctl', 'daemon-reload'): '',
            }
            self.assertIn(args, outputs)
            return subprocess.CompletedProcess(args, 0, outputs[args], '')
        def guest_text(path, *args, **kwargs):
            values = {'/etc/hostname': 'native-guards\n', '/etc/os-release': 'VERSION_ID="24.04"\n'}
            self.assertIn(str(path), values)
            return values[str(path)]
        # Only the host/guest entry boundary is doubled. Execute real main and
        # set_overrides, writing only owned temporary override files. No VM,
        # systemd command, signal or kernel identity operation can run here.
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            for target, name, options in (
                (module.sys, 'platform', {'new': 'linux'}),
                (module.os, 'geteuid', {'return_value': 0}),
                (module.os, 'pidfd_open', {'create': True}),
                (module.signal, 'pidfd_send_signal', {'create': True}),
                (module.Path, 'read_text', {'new': guest_text}),
                (module.Path, 'is_file', {'return_value': True}),
                (module, 'run', {'side_effect': external_command}),
                (module, 'load_bootstrap', {'return_value': bootstrap}),
                (module, 'setup', {'return_value': config}),
                (module, 'observe_worker_startup', {'return_value': {'ready': True}}),
                (module, 'prove_rejection', {}),
                (module, 'properties', {'return_value': observed}),
                (module, 'override_path', {'side_effect': lambda unit: Path(directory) / unit}),
            ):
                stack.enter_context(patch.object(target, name, **options))
            signal_call = stack.enter_context(patch.object(module, 'signal_once'))
            identity_call = stack.enter_context(patch.object(module, 'api_identity'))
            health_call = stack.enter_context(patch.object(module.urllib.request, 'build_opener',
                side_effect=AssertionError('failed override gate must stop before health')))
            stack.enter_context(redirect_stdout(output))
            with self.assertRaises(AssertionError):
                module.main()
            signal_call.assert_not_called()
            identity_call.assert_not_called()
            health_call.assert_not_called()
        records = [json.loads(line.removeprefix('NATIVE OVERRIDE CHECK '))
                   for line in output.getvalue().splitlines() if line.startswith('NATIVE OVERRIDE CHECK ')]
        self.assertEqual(records, [{'phase': 'set_overrides', 'unit': module.UNITS[0],
                                    'properties': {key: observed[key] for key in self.EXPECTED},
                                    'differing_keys': ['Restart', 'WatchdogUSec']}])
        self.assertNotIn('never-log-this', output.getvalue())
        self.assertNotIn('not-selected', output.getvalue())


if __name__ == '__main__':
    unittest.main()
