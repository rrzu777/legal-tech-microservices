"""Integral real-systemd apply/postflight/manual+automatic rollback in QEMU only."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import stat
import subprocess

REPO = Path('/opt/legal-tech-microservices')
LAB = Path('/opt/native-fixture')


def run(*args, env=None, check=True):
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=900)
    if check and result.returncode:
        # This guest contains only generated doubles; still keep errors bounded.
        print(result.stdout[-2000:], result.stderr[-2000:], flush=True)
        raise RuntimeError('Native command failed: ' + args[0])
    return result


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
    shutil.copytree('/mnt/payload/ops', REPO / 'ops', dirs_exist_ok=True)
    run('git', '-C', str(REPO), 'add', 'ops')
    run('git', '-C', str(REPO), '-c', 'user.name=Native Fixture',
        '-c', 'user.email=native@example.invalid', 'commit', '--allow-empty', '-qm', 'reviewed native payload')
    sha = run('git', '-C', str(REPO), 'rev-parse', 'HEAD').stdout.strip()
    env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}
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
    baseline = snapshot()
    (LAB / 'baseline.json').write_text(json.dumps(baseline, sort_keys=True))
    script = str(REPO / 'ops/resource-guards.sh')

    def guard(*arguments, environment=env, expected=0):
        print('NATIVE ' + ' '.join(arguments[:1]), flush=True)
        result = run(script, *arguments, env=environment, check=False)
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

    guard('preflight', '--expected-sha', sha)
    success = guard('apply', '--expected-sha', sha)
    guard('postflight')
    state = json.loads(Path('/var/lib/legaltech-monitor/state-local.json').read_text())
    assert state['delivery_mode'] == 'local'
    active = [name for name, value in state['rules'].items() if value.get('active_since')]
    print('Native active alert keys: ' + json.dumps(active), flush=True)
    assert not active, 'Native monitor reported unhealthy/unknown telemetry'
    assert Path('/var/log/legaltech/resources.csv').stat().st_size > 0
    backup = success.stdout.split('APPLY OK; backup: ', 1)[1].splitlines()[0]
    guard('rollback', '--backup-dir', backup)
    assert snapshot() == baseline, 'Native manual rollback differs from baseline'
    print('NATIVE MANUAL ROLLBACK EXACT', flush=True)

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
    print('NATIVE AUTOMATIC ROLLBACK EXACT; INTEGRAL PASS', flush=True)


if __name__ == '__main__':
    main()
