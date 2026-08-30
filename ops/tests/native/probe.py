"""Characterize actual systemd contracts in the disposable QEMU guest only."""
from pathlib import Path
import os
import hashlib
import re
import subprocess


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Requires the dedicated ARM QEMU guest')
    if Path('/etc/hostname').read_text().strip() != 'native-guards':
        raise SystemExit('Not the native validation guest')
    assert run('systemd-detect-virt').stdout.strip() == 'qemu'
    assert 'VERSION_ID="24.04"' in Path('/etc/os-release').read_text()
    version = run('systemctl', '--version').stdout.splitlines()[0]
    assert version.startswith('systemd 255 ')
    print(version)
    source = Path('/mnt/payload/ops/resource-guards.sh').read_text()
    print('resource-guards sha256=' + hashlib.sha256(source.encode()).hexdigest())
    names = ('fail', 'show_contract', 'scoped_systemctl', 'read_unit_state',
             'read_correlated_unit_activity', 'read_monitor_restore_activity')
    functions = []
    for name in names:
        match = re.search(r'^' + name + r'\(\) \{.*?^\}', source, re.M | re.S)
        assert match, name
        functions.append(match.group())
    typed = re.search(r'^verify_tracker_environment_files\(\) \{.*?^\}', source, re.M | re.S)
    if typed:
        functions.append(typed.group())
    helper = Path('/run/native-contract-functions.sh')
    helper.write_text('systemctl_bin=/usr/bin/systemctl\nbusctl_bin=/usr/bin/busctl\n'
                      'null_file=/dev/null\nEXIT_ERROR=1\n' + '\n'.join(functions))
    base = Path('/run/systemd/system')
    tracker = base / 'legaltech-resource-tracker.service'
    timer = base / 'legaltech-resource-tracker.timer'
    assert not tracker.exists() and not timer.exists()
    tracker.write_text('[Service]\nType=oneshot\nExecStart=/usr/bin/true\n')
    run('systemctl', 'daemon-reload')
    print(run('systemctl', 'show', tracker.name, '--all',
              '-p', 'EnvironmentFiles', '-p', 'RestrictAddressFamilies').stdout.strip())
    print(run('busctl', 'get-property', 'org.freedesktop.systemd1',
              '/org/freedesktop/systemd1/unit/legaltech_2dresource_2dtracker_2eservice',
              'org.freedesktop.systemd1.Service', 'EnvironmentFiles').stdout.strip())
    checks = [
        ('empty-environment', 'verify_tracker_environment_files' if typed else
         'show_contract legaltech-resource-tracker.service EnvironmentFiles ""'),
        ('unrestricted-address-families', 'show_contract legaltech-resource-tracker.service RestrictAddressFamilies "~"'),
    ]
    # Removing a running timer's unit file yields not-found + failed + rc=4.
    timer.write_text('[Timer]\nOnBootSec=1h\nUnit=legaltech-resource-tracker.service\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'start', timer.name)
    timer.unlink()
    run('systemctl', 'daemon-reload')
    print(run('systemctl', 'show', timer.name, '-p', 'LoadState', '-p', 'ActiveState').stdout.strip())
    active = run('systemctl', 'is-active', timer.name, check=False)
    print(f'is-active={active.stdout.strip()} rc={active.returncode}')
    assert active.returncode == 4 and active.stdout.strip() == 'failed'
    checks.append(('removed-failed-timer',
                   'test "$(read_monitor_restore_activity legaltech-resource-tracker.timer absent 1)" = inactive'))
    failed = 0
    try:
        for name, command in checks:
            result = run('bash', '-c', f'source {helper}; {command}', check=False)
            print(f'{name}: {"PASS" if result.returncode == 0 else "FAIL"}', flush=True)
            failed += result.returncode != 0
    finally:
        run('systemctl', 'stop', timer.name, check=False)
        run('systemctl', 'reset-failed', timer.name, check=False)
        tracker.unlink()
        run('systemctl', 'daemon-reload')
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
