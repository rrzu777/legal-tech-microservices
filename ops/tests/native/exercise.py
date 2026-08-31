"""Integral real-systemd apply/postflight/manual+automatic rollback in QEMU only."""
from pathlib import Path
import hashlib
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

REPO = Path('/opt/legal-tech-microservices')
LAB = Path('/opt/native-fixture')
LOGS = REPO / 'estrado-pjud-service/logs'
CLI = REPO / 'ops/worker-maintenance.py'


def run(*args, env=None, check=True, fds=()):
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=900, pass_fds=fds)
    if check and result.returncode:
        # This guest contains only generated doubles; still keep errors bounded.
        print(result.stdout[-2000:], result.stderr[-2000:], flush=True)
        raise RuntimeError('Native command failed: ' + args[0])
    return result


def run_observed(*args, during, env=None):
    with tempfile.TemporaryFile(mode='w+') as output, tempfile.TemporaryFile(mode='w+') as errors:
        process = subprocess.Popen(args, env=env, stdout=output, stderr=errors, start_new_session=True)
        try:
            during(process)
            process.wait(timeout=900)
            output.seek(0)
            errors.seek(0)
            return subprocess.CompletedProcess(args, process.returncode, output.read(), errors.read())
        except BaseException:
            # Generated/local fixture data only. Retain bounded command evidence
            # in integral.log before the runner destroys the disposable guest.
            output.seek(0)
            errors.seek(0)
            print('NATIVE interrupted observer diagnostics:\n' + output.read()[-4000:] + errors.read()[-4000:], flush=True)
            raise
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def until(predicate, *, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError('Bounded native fixture observation timed out')


def control():
    return json.loads(Path('/var/lib/worker-maintenance/control.json').read_text())


def request(action, expected):
    key = str(uuid.uuid4())
    temporary = LOGS / 'request.tmp'
    temporary.write_text(json.dumps({'id': key, 'action': action}))
    temporary.replace(LOGS / 'request.json')
    def observed():
        path = LOGS / 'result.json'
        value = json.loads(path.read_text()) if path.is_file() else {}
        return value if value.get('id') == key else None
    value = until(observed)
    assert value['outcome'] == expected, 'Unexpected local admission outcome'
    return value


def verify_identity(status, ack, mainpid, *, previous=None):
    state, operation, identity = status.split()
    boot, pid, ticks, nonce = identity.split(':')
    assert ack['operation_id'] == operation
    assert (ack['boot_id'], ack['pid'], ack['start_ticks'], ack['instance_id']) == (boot, int(pid), int(ticks), nonce)
    assert mainpid == int(pid), 'systemd MainPID must be the ACK publisher, not xvfb-run'
    assert ack['inflight'] == 0
    assert ack['state'] == ('quiescent' if state == 'hold' else 'draining')
    if previous:
        old_boot, old_pid, old_ticks, old_nonce = previous.split(':')
        assert nonce != old_nonce and (boot, pid, ticks) != (old_boot, old_pid, old_ticks)
    return identity


def prove_worker(state, *, previous=None):
    # CLI supplies the actual kernel/cgroup/metadata authentication, not fixture
    # inference. This additional equality proves the xvfb-run handoff itself.
    def ready():
        result = run(sys.executable, str(CLI), 'status', check=False)
        if result.returncode != 0:
            return None
        status = result.stdout.strip()
        try:
            assert status.split()[0] == state
            ack = json.loads(Path('/run/worker-maintenance/ack.json').read_text())
            mainpid = int(run('systemctl', 'show', 'estrado-pjud-worker.service', '--property=MainPID', '--value').stdout)
            identity = verify_identity(status, ack, mainpid, previous=previous)
            assert b'fixture_worker.py' in Path(f'/proc/{mainpid}/cmdline').read_bytes()
            parent = Path(f'/proc/{mainpid}/stat').read_text().rsplit(')', 1)[1].split()[1]
            assert b'xvfb-run' in Path(f'/proc/{parent}/cmdline').read_bytes(), 'Fixture did not use real xvfb-run'
        except AssertionError:
            # Publishing open can precede the next same-identity draining ACK.
            # Retry the entire authenticated observation, never weaken proof or
            # accept the stale ACK. Persistent mismatches exhaust until's bound.
            return None
        return identity
    identity = until(ready)
    print('NATIVE IDENTITY ' + state + ' ' + identity, flush=True)
    return identity


def release_validated():
    identity = prove_worker('hold')
    operation = control()['operation_id']
    # After exact rollback restoration the legacy resource layout intentionally
    # cannot pass guards postflight. This explicit low-level release still
    # requires current ACK, EX, API health and a durable succeeded journal.
    run(sys.executable, str(CLI), 'finish', '--operation-id', operation, '--identity', identity)
    prove_worker('open')
    request('probe', 'admitted')


def stale_proofs(store, identity):
    from dataclasses import replace
    operation = control()['operation_id']
    pid = int(identity.split(':')[1])
    original = store.read_ack_candidate()
    os.kill(pid, signal.SIGSTOP)
    try:
        until(lambda: Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'T')
        for kind, changes in (
                ('operation', {'operation_id': str(uuid.uuid4())}),
                ('pid', {'pid': pid + 100000}),
                ('ticks', {'start_ticks': original.start_ticks + 1}),
                ('boot', {'boot_id': str(uuid.uuid4())}),
                ('nonce', {'instance_id': str(uuid.uuid4())})):
            store.write_ack(replace(original, **changes))
            rejected = run(sys.executable, str(CLI), 'verify-ack', '--operation-id', operation,
                           '--identity', identity, check=False)
            assert rejected.returncode == 1 and control()['state'] == 'hold'
            print('NATIVE STALE REJECTED ' + kind, flush=True)
    finally:
        store.write_ack(original)
        os.kill(pid, signal.SIGCONT)


def helper_death_and_closed_restart(environment):
    from worker.maintenance_store import MaintenanceStore
    helper_log = LAB / 'helper.log'
    body = '''set -e
source /opt/legal-tech-microservices/ops/worker-maintenance.sh
wm_init
wm_acquire_global
wm_prepare
printf 'DRAINED\\n'
exec /usr/bin/sleep 600
'''
    with helper_log.open('w') as output:
        helper = subprocess.Popen(['/bin/bash', '-c', body], env=environment,
                                  stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            until(lambda: 'DRAINED' in helper_log.read_text())
            identity = prove_worker('hold')
            operation = control()['operation_id']
            os.killpg(helper.pid, signal.SIGKILL)
            assert helper.wait(timeout=5) == -signal.SIGKILL
        except BaseException:
            print('NATIVE helper diagnostics:\n' + helper_log.read_text()[-4000:], flush=True)
            raise
        finally:
            if helper.poll() is None:
                os.killpg(helper.pid, signal.SIGKILL)
                helper.wait(timeout=5)
    request('probe', 'blocked')
    store = MaintenanceStore.production(operator=True)
    stale_proofs(store, identity)
    # Explicit lab restart under the same real global/admission leases, with
    # own journaled drain. No legacy/PID0 fallback, no automatic release.
    with open('/run/lock/legaltech-resource-guards.lock', 'rb') as global_lock:
        fcntl.flock(global_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with store.exclusive_lease() as admission:
            descriptors = (global_lock.fileno(), admission)
            flags = ['--global-fd', str(descriptors[0]), '--admission-fd', str(descriptors[1])]
            run(sys.executable, str(CLI), 'verify-ack', '--operation-id', operation,
                '--identity', identity, *flags, fds=descriptors)
            marker = Path('/run/worker-maintenance/old-runtime-marker')
            marker.touch()
            run('systemctl', 'restart', 'estrado-pjud-worker.service')
            assert not marker.exists(), 'RuntimeDirectory was not recreated'
            current = prove_worker('hold', previous=identity)
            run(sys.executable, str(CLI), 'verify-ack', '--operation-id', operation,
                '--new-instance-from', identity, *flags, fds=descriptors)
            assert control()['state'] == 'hold'
            request('probe', 'blocked')
    release_validated()
    print('NATIVE HELPER DEATH HOLD; CLOSED RESTART VERIFIED ' + current, flush=True)


def inspect_path(path, *, hash_contents=True):
    # Check ancestors too: exists() would mistake dangling links for absence.
    for entry in (*reversed(path.parents), path):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError('Unexpected symlink in native snapshot')
    digest = None
    if stat.S_ISREG(info.st_mode) and hash_contents and path.name not in ('.env', 'legaltech-monitoring.env'):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, digest]


def snapshot():
    systemd = Path('/etc/systemd/system')
    paths = [systemd / name for name in (
        'legaltech.slice', 'estrado-pjud.service', 'estrado-pjud-worker.service',
        'estrado-pjud-worker.service.d/xvfb.conf', 'legaltech-monitor.service',
        'legaltech-resource-tracker.service', 'legaltech-monitor.timer',
        'legaltech-resource-tracker.timer', 'user-1002.slice.d/50-legaltech-resource-limits.conf')]
    paths += [REPO / 'estrado-pjud-service/.env', Path('/etc/legaltech-monitoring.env'),
              Path('/etc/fstab'), Path('/etc/sysctl.d/60-legaltech-swap.conf'),
              Path('/etc/sysctl.d/60-legaltech-swap.previous'),
              Path('/etc/logrotate.d/legaltech-resources'), Path('/opt/legaltech-monitoring'),
              Path('/swapfile')]
    result = {}
    for path in paths:
        metadata = inspect_path(path, hash_contents=path != Path('/swapfile'))
        entries = [path] + (sorted(path.rglob('*')) if metadata and stat.S_ISDIR(metadata[0]) else [])
        for entry in entries:
            result[str(entry)] = inspect_path(entry, hash_contents=entry != Path('/swapfile'))
    units = {}
    for unit in ('estrado-pjud.service', 'estrado-pjud-worker.service',
                 'legaltech-monitor.service', 'legaltech-resource-tracker.service',
                 'legaltech-monitor.timer', 'legaltech-resource-tracker.timer',
                 'hermes-gateway.service', 'hermes-dashboard.service'):
        prefix = ['systemctl']
        if unit.startswith('hermes-'):
            prefix += ['--user', '--machine=hermes@.host']
        units[unit] = [run(*prefix, action, unit, check=False).stdout.strip()
                       for action in ('is-active', 'is-enabled')]
    result['units'] = units
    result['worker-cgroup'] = run('systemctl', 'show', 'estrado-pjud-worker.service',
                                   '--property=ControlGroup', '--value').stdout.strip()
    result['swap'] = Path('/proc/swaps').read_text()
    result['swappiness'] = Path('/proc/sys/vm/swappiness').read_text()
    return result


def main():
    if os.geteuid() != 0 or Path('/etc/hostname').read_text().strip() != 'native-guards':
        raise SystemExit('Not the isolated validation guest')
    if run('systemd-detect-virt').stdout.strip() != 'qemu' or not (LAB / 'environment.json').is_file():
        raise SystemExit('Requires the initialized QEMU fixture')
    if run('git', '-C', str(REPO), 'status', '--porcelain').stdout or Path('/proc/swaps').read_text().count('\n') != 1:
        raise SystemExit('Refusing dirty/reused native baseline')
    if (LAB / 'exercise-started').exists():
        raise SystemExit('Refusing blind reuse of an integral exercise')
    (LAB / 'exercise-started').touch()
    sys.path.insert(0, str(REPO / 'estrado-pjud-service'))
    from worker.maintenance_store import AdmissionClosed, MaintenanceStore
    shutil.copytree('/mnt/payload/ops', REPO / 'ops', dirs_exist_ok=True)
    run('git', '-C', str(REPO), 'add', 'ops')
    run('git', '-C', str(REPO), '-c', 'user.name=Native Fixture',
        '-c', 'user.email=native@example.invalid', 'commit', '--allow-empty', '-qm', 'reviewed native payload')
    sha = run('git', '-C', str(REPO), 'rev-parse', 'HEAD').stdout.strip()
    env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
           'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}
    env.update(json.loads((LAB / 'environment.json').read_text()))
    env['RG_TEST_MODE'] = '1'
    # Capture the real provisioner's safe diagnostics before this disposable
    # guest is destroyed. No extra provision attempt and no changed exit status.
    provision_capture = LAB / 'provision-capture'
    provision_capture.write_text('#!/bin/sh\nexec /opt/legal-tech-microservices/ops/provision.sh "$@" > /opt/native-fixture/provision.log 2>&1\n')
    provision_capture.chmod(0o755)
    env['RG_PROVISION_BIN'] = str(provision_capture)
    # Only the guest's clock changes. No host clock access or real API exists.
    run('timedatectl', 'set-ntp', 'false')
    run('date', '-s', '2026-08-31 01:00:00 UTC')
    prove_worker('open')
    # Root-owned control is readable but not writable by the actual worker UID.
    for name in ('control.json', 'admission.lock', 'unauthorized-new-file'):
        denied = run('runuser', '-u', 'estrado', '--', '/usr/bin/python3', '-c',
                     "import os,sys\ntry: os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT, 0o600)\n"
                     "except PermissionError: print('denied')\nelse: sys.exit(1)",
                     '/var/lib/worker-maintenance/' + name)
        assert denied.stdout.strip() == 'denied'
    initial_control = control()
    lock_path = Path('/var/lib/worker-maintenance/admission.lock')
    lock_metadata = inspect_path(lock_path)
    lock_inode = lock_path.stat().st_ino
    helper_death_and_closed_restart(env)
    baseline = snapshot()
    (LAB / 'baseline.json').write_text(json.dumps(baseline, sort_keys=True))
    script = str(REPO / 'ops/resource-guards.sh')

    def guard(*arguments, environment=env, expected=0, during=None):
        print('NATIVE ' + ' '.join(arguments[:1]), flush=True)
        result = (run_observed(script, *arguments, env=environment, during=during) if during else
                  run(script, *arguments, env=environment, check=False))
        print(result.stdout, result.stderr, flush=True)
        if result.returncode != expected:
            diagnostic = LAB / 'provision.log'
            if diagnostic.is_file():
                print('NATIVE provision diagnostics:\n' + diagnostic.read_text()[-6000:], flush=True)
            required = ('systemd', 'env.inventory', 'systemd-templates/hermes-user.slice.conf',
                        'logrotate/legaltech-resources', 'monitoring/monitor.py',
                        'monitoring/resource-tracker.py', 'monitoring/alert_policy.py',
                        'monitoring/resource_metrics.py')
            print('NATIVE required source metadata: ' + json.dumps({name: {
                'exists': (REPO / 'ops' / name).exists(),
                'readable': os.access(REPO / 'ops' / name, os.R_OK)} for name in required}), flush=True)
            for folder in ('systemd-templates', 'monitoring', 'logrotate'):
                directory = REPO / 'ops' / folder
                if directory.is_dir():
                    print('NATIVE source names ' + folder + ': ' + json.dumps(sorted(p.name for p in directory.iterdir())), flush=True)
            if 'ROLLBACK OK:' in result.stdout + result.stderr:
                current = snapshot()
                changed = sorted(key for key in current.keys() | baseline.keys()
                                 if current.get(key) != baseline.get(key))
                print('NATIVE unexpected-failure rollback changed paths: ' + json.dumps(changed), flush=True)
                assert not changed, 'Unexpected-failure rollback differs from baseline'
        assert result.returncode == expected, f'Unexpected native guard rc={result.returncode}'
        return result

    # A genuinely incompatible installed rollback unit is rejected before hold,
    # backup, stop or provisioning. Restoring the fixture is an explicit lab step.
    unit = Path('/etc/systemd/system/estrado-pjud-worker.service')
    compatible = unit.read_text()
    before_legacy = control()
    try:
        unit.write_text(compatible.replace('RuntimeDirectory=worker-maintenance\n', ''))
        run('systemctl', 'daemon-reload')
        rejected_baseline = snapshot()
        guard('apply', '--expected-sha', sha, expected=1)
        assert control() == before_legacy and snapshot() == rejected_baseline
        assert not (LAB / 'provision.log').exists()
    finally:
        unit.write_text(compatible)
        run('systemctl', 'daemon-reload')
    assert snapshot() == baseline
    print('NATIVE LEGACY UNIT REJECTED BEFORE MUTATION', flush=True)

    # Missing capability is not an invitation to bootstrap or invent an ACK.
    identity = prove_worker('open')
    store = MaintenanceStore.production(operator=True)
    ack_path = Path('/run/worker-maintenance/ack.json')
    os.kill(int(identity.split(':')[1]), signal.SIGSTOP)
    original = store.read_ack_candidate()
    try:
        until(lambda: Path(f'/proc/{identity.split(":")[1]}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'T')
        ack_path.unlink()
        guard('apply', '--expected-sha', sha, expected=1)
        assert control() == before_legacy and snapshot() == baseline
        assert not (LAB / 'provision.log').exists()
    finally:
        store.write_ack(original)
        os.kill(int(identity.split(':')[1]), signal.SIGCONT)
    print('NATIVE MISSING LEGACY ACK REJECTED BEFORE MUTATION', flush=True)

    def observe_drain(process):
        until(lambda: control()['state'] == 'hold' or process.poll() is not None)
        assert process.poll() is None, 'Guard exited before drain observation'
        blocked = request('probe', 'blocked')
        ack = json.loads(ack_path.read_text())
        assert ack['pid'] == blocked['pid'] == admitted_pid
        assert ack['inflight'] == 1 and ack['state'] == 'draining'
        try:
            with MaintenanceStore.production(operator=True).exclusive_lease():
                raise AssertionError('Guard or fixture released inflight SH prematurely')
        except AdmissionClosed:
            pass
        assert Path(f'/proc/{admitted_pid}').exists(), 'Admitted work was killed during hold'
        request('release', 'released')

    guard('preflight', '--expected-sha', sha)
    admitted_pid = request('start', 'started')['pid']
    success = guard('apply', '--expected-sha', sha, during=observe_drain)
    applied_identity = prove_worker('open', previous=identity)
    guard('postflight')
    state = json.loads(Path('/var/lib/legaltech-monitor/state-local.json').read_text())
    assert state['delivery_mode'] == 'local'
    active = [name for name, value in state['rules'].items() if value.get('active_since')]
    print('Native active alert keys: ' + json.dumps(active), flush=True)
    assert not active, 'Native monitor reported unhealthy/unknown telemetry'
    assert Path('/var/log/legaltech/resources.csv').stat().st_size > 0
    backup = success.stdout.split('APPLY OK; backup: ', 1)[1].splitlines()[0]
    admitted_pid = request('start', 'started')['pid']
    guard('rollback', '--backup-dir', backup, during=observe_drain)
    assert snapshot() == baseline, 'Native manual rollback differs from baseline'
    prove_worker('hold', previous=applied_identity)
    request('probe', 'blocked')
    assert inspect_path(lock_path) == lock_metadata and lock_path.stat().st_ino == lock_inode
    print('NATIVE MANUAL ROLLBACK EXACT', flush=True)
    release_validated()

    # One HTTP health failure at final postflight, after real provisioning and
    # real sandbox execution. Heartbeats/claim reads still use the local double.
    fault = LAB / 'curl-fault'
    fault.write_text('''#!/bin/bash
if [ "${1:-}" != --config ]; then
  count=0
  [ ! -f /opt/native-fixture/health-count ] || read -r count < /opt/native-fixture/health-count
  count=$((count + 1))
  printf '%s\\n' "$count" > /opt/native-fixture/health-count
  if [ "$count" = 3 ]; then
    touch /opt/native-fixture/health-fault-injected
    printf 503
    exit 0
  fi
fi
exec /usr/bin/curl "$@"
''')
    fault.chmod(0o755)
    failed = guard('apply', '--expected-sha', sha,
                   environment=env | {'RG_CURL_BIN': str(fault)}, expected=1)
    assert 'ROLLBACK OK' in failed.stdout + failed.stderr
    assert (LAB / 'health-fault-injected').is_file(), 'Intended HTTP fault was not reached'
    assert 'apply failed in phase: postflight' in failed.stderr
    assert snapshot() == baseline, 'Native automatic rollback differs from baseline'
    prove_worker('hold')
    request('probe', 'blocked')
    assert control()['operation_id'] != initial_control['operation_id']
    assert inspect_path(lock_path) == lock_metadata and lock_path.stat().st_ino == lock_inode
    release_validated()
    print('NATIVE AUTOMATIC ROLLBACK EXACT; REAL MAINTENANCE INTEGRAL PASS', flush=True)


if __name__ == '__main__':
    main()
