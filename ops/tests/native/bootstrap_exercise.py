"""Real guest shutdown characterization only. Never claims bootstrap completion."""
from pathlib import Path
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
import pwd
import grp
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.request
import uuid
from types import CodeType, FunctionType
from zoneinfo import ZoneInfo

REPO = Path('/opt/legal-tech-microservices')
APP = REPO / 'estrado-pjud-service'
LAB = Path('/opt/native-bootstrap-characterization')
UNITS = ('estrado-pjud.service', 'estrado-pjud-worker.service')
MODE = LAB / 'lifespan-mode'
OVERRIDE = '[Service]\nRestart=no\nWatchdogSec=0\nSendSIGKILL=no\nTimeoutStopSec=infinity\n'
ENV = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
ALLOW_DAYTIME_LAB = False
# Fixture-only binding to the reviewed installer, not a production protocol.
INSTALLER_SHA256 = '7d37c9f7eb3d4f5650c5de867321b217abc1b7e95700fccedae4c941508320ad'
STOPPED_UNIT_STATE_LINE = 216
PING_FILE = APP / 'logs/native-bootstrap-watchdog/latest.json'
WATCHDOG_KEYS = ('Type', 'NotifyAccess', 'TimeoutStartUSec', 'ControlPID', 'PropagatesReloadTo',
                 'ReloadPropagatedFrom', 'TriggeredBy', 'NeedDaemonReload', 'LoadState', 'ActiveState',
                 'SubState', 'Result', 'MainPID', 'ControlGroup', 'InvocationID', 'FragmentPath',
                 'DropInPaths', 'UnitFileState', 'Restart', 'WatchdogUSec', 'SendSIGKILL',
                 'TimeoutStopUSec', 'Job')


def require_lab_guest():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise SystemExit('Requires isolated Linux/root QEMU guest')
    assert Path('/etc/hostname').read_text().strip() == 'native-guards'
    assert run('/usr/bin/systemd-detect-virt').stdout.strip() == 'qemu'


def lab_window(bootstrap, config):
    # Explicit lab policy only: never mutate Config.clock or the real installer.
    if ALLOW_DAYTIME_LAB:
        require_lab_guest()
    else:
        bootstrap.window(config)


def cli_rejection_evidence():
    now = datetime.now(timezone.utc).astimezone(ZoneInfo('America/Santiago'))
    return dict(actual_santiago=now.isoformat(),
        production_window_open_at_observation=now.hour >= 20 or now.hour < 4,
        production_window_unchanged=True, cli_rejection_gate='not_exposed',
        daytime_lab_opt_in=ALLOW_DAYTIME_LAB)


def asgi_source(mode_path):
    return '''from pathlib import Path
async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                print("NATIVE_SYNTHETIC_SHUTDOWN_ENTERED", flush=True)
                if Path(MODE_PATH).read_text() == "lifespan_error":
                    print("NATIVE_SYNTHETIC_CLEANUP_FAILED", flush=True)
                    raise RuntimeError("synthetic lifespan cleanup failure")
                await send({"type": "lifespan.shutdown.complete"})
                print("NATIVE_SYNTHETIC_SHUTDOWN_COMPLETE", flush=True)
                return
    elif scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
'''.replace('MODE_PATH', repr(str(mode_path)))


def synthetic_units(ops):
    result = {}
    for name in UNITS:
        body = (ops / 'systemd' / name).read_text()
        line = 'EnvironmentFile=/opt/legal-tech-microservices/estrado-pjud-service/.env\n'
        assert body.count(line) == 1
        body = body.replace(line, '')
        if name == UNITS[1]:
            runtime = 'RuntimeDirectory=worker-maintenance\nRuntimeDirectoryMode=0700\n'
            assert body.count(runtime) == 1
            body = body.replace(runtime, '')
        result[name] = body
    return result


def legacy_source(ping_path):
    return '''import json, os, signal, socket, time
from pathlib import Path
from worker.sd_notify import notify_ready
stopping = False
def stop(signum, frame):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
path = Path(PING_PATH)
temporary = path.with_name(".latest.tmp")
records = []
sequence = 0
address = os.environ["NOTIFY_SOCKET"]
if address.startswith("@"):
    address = "\\0" + address[1:]
with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
    sock.connect(address)
    while not stopping:
        sequence += 1
        # Timestamp BEFORE send: a record newer than the controller's barrier
        # observation cannot describe a ping sent before that observation.
        stamp = time.monotonic_ns()
        count = sock.send(b"WATCHDOG=1")
        assert count == 10
        records = (records + [dict(sequence=sequence, monotonic_ns=stamp,
                                  pid=os.getpid(), sent_bytes=count)])[-2:]
        if len(records) == 2:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(records, stream)
            os.replace(temporary, path)
            if sequence == 2:
                notify_ready()
        time.sleep(.25)
print("NATIVE_SYNTHETIC_LEGACY_WORKER_EXIT_ZERO", flush=True)
'''.replace('PING_PATH', repr(str(ping_path)))


def shutdown_evidence(case, values, journal):
    assert case in ('normal', 'lifespan_error')
    assert all(values[key] == expected for key, expected in {
        'ExecMainCode': '1', 'ExecMainStatus': '143', 'Result': 'exit-code',
        'MainPID': '0', 'ActiveState': 'failed', 'SubState': 'failed',
    }.items()), 'Unrecognized real API shutdown snapshot; do not normalize it'
    # The real CLI keeps lifespan=auto. Uvicorn can log "shutdown complete"
    # after a lifespan RuntimeError in auto; only this synthetic body supplies
    # ground truth. These markers are NOT a new production acceptance signal.
    complete = 'NATIVE_SYNTHETIC_SHUTDOWN_COMPLETE' in journal
    failed = 'NATIVE_SYNTHETIC_CLEANUP_FAILED' in journal
    assert (complete, failed) == ((True, False) if case == 'normal' else (False, True))
    return dict(case=case, api_properties=values, lifespan_completed=complete,
                lifespan_failed=failed, uvicorn_reported_complete='Application shutdown complete.' in journal,
                uvicorn_reported_failed='Application shutdown failed. Exiting.' in journal,
                uvicorn_reported_unsupported="ASGI 'lifespan' protocol appears unsupported." in journal,
                exit_status_is_not_clean_shutdown_proof=True)


def run(*args, check=True, timeout=30):
    value = subprocess.run(args, env=ENV, check=False, text=True, capture_output=True, timeout=timeout)
    if check and value.returncode:
        print(value.stdout[-4000:] + value.stderr[-4000:], flush=True)
        raise RuntimeError('Native characterization command failed: ' + args[0])
    return value


def write_new(path, body, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        stream.write(body)
    path.chmod(mode)


def load_bootstrap(path):
    spec = importlib.util.spec_from_file_location('native_bootstrap_installer', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def until(predicate, label, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Native observation timed out without escalation: ' + label)


def properties(unit, keys=None, *, timeout=30):
    keys = keys or ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'ExecMainPID', 'ExecMainCode',
            'ExecMainStatus', 'ExecMainExitTimestampMonotonic', 'ControlGroup', 'Slice', 'Result',
            'UnitFileState', 'Job', 'InvocationID', 'Restart', 'WatchdogUSec', 'SendSIGKILL',
            'TimeoutStopUSec', 'SuccessExitStatus')
    lines = run('/usr/bin/systemctl', 'show', unit, *(f'--property={key}' for key in keys),
                timeout=timeout).stdout.splitlines()
    pairs = [line.split('=', 1) for line in lines]
    assert len(pairs) == len(keys) and len({key for key, value in pairs}) == len(keys)
    result = dict(pairs)
    assert set(result) == set(keys)
    return result


def group_pids(group):
    assert group in {f'/legaltech.slice/{name}' for name in UNITS}
    root = Path('/sys/fs/cgroup') / group.lstrip('/')
    if not root.exists():
        return []
    result = []
    for directory, children, _ in os.walk(root, followlinks=False):
        assert not Path(directory).is_symlink()
        assert all(not (Path(directory) / child).is_symlink() for child in children)
        result += [int(item) for item in (Path(directory) / 'cgroup.procs').read_text().split()]
    return sorted(set(result))


def identity(pid):
    path = Path('/proc') / str(pid)
    stat = (path / 'stat').read_text()
    assert stat.startswith(str(pid) + ' (')
    fields = stat.rsplit(')', 1)[1].split()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert str(uuid.UUID(boot)) == boot
    return dict(pid=pid, parent_pid=int(fields[1]), start_ticks=int(fields[19]), boot_id=boot,
                uid=path.stat().st_uid, cgroup=(path / 'cgroup').read_text().strip())


def commandline(pid):
    return (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')[:-1]


def api_identity():
    values = properties(UNITS[0])
    assert values['ActiveState'] == 'active' and values['SubState'] == 'running'
    group = values['ControlGroup']
    wrapper = int(values['MainPID'])
    members = group_pids(group)
    assert wrapper in members and b'/usr/bin/xvfb-run' in commandline(wrapper)
    candidates = [pid for pid in members if pid != wrapper and
                  str(APP / '.venv/bin/uvicorn').encode() in commandline(pid)]
    assert len(candidates) == 1
    child = identity(candidates[0])
    assert child['parent_pid'] == wrapper and child['uid'] == pwd.getpwnam('www-data').pw_uid
    assert child['cgroup'] == '0::' + group and b'app.main:app' in commandline(child['pid'])
    return dict(wrapper=identity(wrapper), child=child, group=group, invocation=values['InvocationID'])


def signal_once(bootstrap, config, observed):
    lab_window(bootstrap, config)
    # Bind the signal to the real kernel process, never a stale/reused numeric PID.
    fd = os.pidfd_open(observed['pid'])
    try:
        assert identity(observed['pid']) == observed
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    finally:
        os.close(fd)


def override_path(unit):
    return Path('/run/systemd/system') / (unit + '.d/90-worker-bootstrap-shutdown.conf')


def watchdog_helper(usec):
    assert usec in (0, 300000000)
    return '[Service]\nTimeoutStartSec=infinity\nExecReload=/usr/bin/systemd-notify WATCHDOG_USEC=' + str(usec) + '\n'


def watchdog_bus_property(name):
    assert name in ('ExecReloadEx', 'WatchdogUSec')
    value = json.loads(run('/usr/bin/busctl', '--system', '--json=short', 'get-property',
        'org.freedesktop.systemd1',
        '/org/freedesktop/systemd1/unit/estrado_2dpjud_2dworker_2eservice',
        'org.freedesktop.systemd1.Service', name).stdout)
    assert set(value) == {'type', 'data'}
    if name == 'WatchdogUSec':
        assert value['type'] == 't' and type(value['data']) is int and value['data'] >= 0
    else:
        assert value['type'] == 'a(sasasttttuii)'
    return value['data']


def check_reload_command(usec):
    # Structured argv AND flags: systemctl-show's space-joined argv loses boundaries.
    rows = watchdog_bus_property('ExecReloadEx')
    assert type(rows) is list
    if usec is None:
        assert rows == [], 'Preexisting/residual ExecReload is forbidden'
        return
    assert len(rows) == 1 and type(rows[0]) is list and len(rows[0]) == 10
    row = rows[0]
    assert row[:3] == ['/usr/bin/systemd-notify',
                       ['/usr/bin/systemd-notify', 'WATCHDOG_USEC=' + str(usec)], []]
    assert all(type(number) is int and number >= 0 for number in row[3:])


def watchdog_snapshot(bootstrap, config, helper_usec, expected_watchdog):
    unit = UNITS[1]
    values = properties(unit, WATCHDOG_KEYS)
    required = dict(Type='notify', NotifyAccess='all', ControlPID='0', PropagatesReloadTo='',
        ReloadPropagatedFrom='', TriggeredBy='', NeedDaemonReload='no', LoadState='loaded',
        ActiveState='active', SubState='running', Result='success', UnitFileState='disabled',
        Restart='no', WatchdogUSec=expected_watchdog, SendSIGKILL='no', TimeoutStopUSec='infinity', Job='')
    if helper_usec is not None:
        required['TimeoutStartUSec'] = 'infinity'
    assert all(values[key] == expected for key, expected in required.items()), 'Unsafe watchdog administrative state'
    assert watchdog_bus_property('WatchdogUSec') == (0 if expected_watchdog == '0' else 300000000)
    drop = override_path(unit)
    helper = drop.with_name('91-native-watchdog-admin.conf')
    xvfb = config.systemd_dir / (unit + '.d/xvfb.conf')
    fragment = config.systemd_dir / unit
    expected_files = {fragment: synthetic_units(REPO / 'ops')[unit], drop: OVERRIDE,
                      xvfb: (REPO / 'ops/systemd' / (unit + '.d/xvfb.conf')).read_text()}
    if helper_usec is not None:
        expected_files[helper] = watchdog_helper(helper_usec)
    else:
        bootstrap.absent(helper)
    assert values['FragmentPath'] == str(fragment)
    actual_drops = values['DropInPaths'].split()
    assert len(actual_drops) == len(expected_files) - 1
    assert set(actual_drops) == {str(path) for path in expected_files if path != fragment}
    for path, body in expected_files.items():
        bootstrap.trusted_ancestors(config, path.parent)
        assert bootstrap.file_text(config, path) == body
    check_reload_command(helper_usec)
    pid = int(values['MainPID'])
    worker = identity(pid)
    assert worker['uid'] == config.worker_uid and worker['cgroup'] == '0::' + values['ControlGroup']
    assert commandline(pid)[-2:] == [b'-m', b'worker']
    wrapper = worker['parent_pid']
    assert b'/usr/bin/xvfb-run' in commandline(wrapper)
    members = group_pids(values['ControlGroup'])
    assert len(members) == 3 and {pid, wrapper}.issubset(members)
    xvfb_pid = next(item for item in members if item not in (pid, wrapper))
    assert Path(os.fsdecode(commandline(xvfb_pid)[0])).name == 'Xvfb'
    identities = [identity(member) for member in members]
    assert all(item['boot_id'] == worker['boot_id'] and item['cgroup'] == worker['cgroup']
               and item['uid'] == config.worker_uid for item in identities)
    assert len(values['InvocationID']) == 32 and uuid.UUID(values['InvocationID']).hex == values['InvocationID']
    return dict(worker=worker, members=identities, invocation=values['InvocationID'], group=values['ControlGroup'])


def watchdog_pings(config):
    parent = PING_FILE.parent.lstat()
    assert stat.S_ISDIR(parent.st_mode) and parent.st_uid == config.worker_uid
    assert not PING_FILE.parent.is_symlink() and stat.S_IMODE(parent.st_mode) == 0o700
    fd = os.open(PING_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode) and info.st_uid == config.worker_uid
        assert info.st_gid == config.worker_gid and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_size <= 2048
        value = json.loads(os.read(fd, 2049))
    finally:
        os.close(fd)
    assert type(value) is list and len(value) == 2
    for item in value:
        assert set(item) == {'sequence', 'monotonic_ns', 'pid', 'sent_bytes'}
        assert all(type(number) is int and number > 0 for number in item.values())
        assert item['sent_bytes'] == 10
    assert value[1]['sequence'] == value[0]['sequence'] + 1
    assert value[1]['monotonic_ns'] >= value[0]['monotonic_ns']
    return value


def startup_worker_snapshot(bootstrap, config, *, timeout=5):
    """Read-only startup state. Only the known xvfb wrapper may be pending."""
    unit = UNITS[1]
    values = properties(unit, bootstrap.PROPERTIES + EXECUTION_KEYS + ('Type', 'NotifyAccess'), timeout=timeout)
    assert all(values[key] == expected for key, expected in dict(Type='notify', NotifyAccess='all',
        LoadState='loaded', NeedDaemonReload='no', UnitFileState='disabled', ActiveState='active',
        SubState='running', Result='success', ControlPID='0', NRestarts='0', Job='',
        Slice='legaltech.slice', ControlGroup='/legaltech.slice/' + unit,
        FragmentPath=str(config.systemd_dir / unit),
        DropInPaths=str(config.systemd_dir / (unit + '.d/xvfb.conf'))).items())
    assert re.fullmatch('[0-9a-f]{32}', values['InvocationID'])
    assert re.fullmatch('[1-9][0-9]{0,9}', values['MainPID'])
    assert re.fullmatch('[1-9][0-9]{0,18}', values['ExecMainStartTimestampMonotonic'])
    # Caller owns EX; opening a second flock description would contend with it.
    files = execution_files(bootstrap, config)
    bootstrap.target_unit(files[0][str(config.systemd_dir / unit)])
    members = [identity(pid) for pid in group_pids(values['ControlGroup'])]
    assert 1 <= len(members) <= 3
    boot = bootstrap.audit.bounded_read(config.proc_root / 'sys/kernel/random/boot_id').strip()
    assert all(member['boot_id'] == boot and member['uid'] == config.worker_uid and
               member['cgroup'] == '0::' + values['ControlGroup'] for member in members)
    commands = {member['pid']: commandline(member['pid']) for member in members}
    wrappers = [member for member in members if b'/usr/bin/xvfb-run' in commands[member['pid']]]
    worker_argv = [str(APP / '.venv/bin/python').encode(), b'-m', b'worker']
    workers = [member for member in members if commands[member['pid']] == worker_argv]
    displays = [member for member in members if commands[member['pid']] and
                Path(os.fsdecode(commands[member['pid']][0])).name == 'Xvfb']
    assert len(wrappers) == 1 and len(workers) <= 1 and len(displays) <= 1
    wrapper = wrappers[0]
    assert len(wrappers + workers + displays) == len(members)
    assert all(member['parent_pid'] == wrapper['pid'] for member in workers + displays)
    assert int(values['ExecMainPID']) in {member['pid'] for member in members}
    main = int(values['MainPID'])
    assert main == wrapper['pid'] or (len(workers) == 1 and main == workers[0]['pid'])
    bootstrap.operator.metadata(PING_FILE.parent.lstat(), config.worker_uid, config.worker_gid, 0o700, True)
    pings = None
    if PING_FILE.exists() or PING_FILE.is_symlink():
        pings = watchdog_pings(config)  # Unsafe/malformed existing data is never pending.
        assert len(workers) == 1 and all(row['pid'] == workers[0]['pid'] for row in pings)
    else:
        bootstrap.absent(PING_FILE)
    ready = main != wrapper['pid']
    if ready:
        assert len(members) == 3 and len(displays) == 1 and pings is not None
    return dict(ready=ready, values=values, wrapper=wrapper, members=members, files=files)


def observe_worker_startup(bootstrap, config):
    deadline = time.monotonic() + 30
    anchor, seen = None, {}
    while time.monotonic() < deadline:
        with bootstrap.global_ex(config):
            observed = startup_worker_snapshot(bootstrap, config, timeout=min(5, deadline - time.monotonic()))
        current = (observed['values']['InvocationID'], observed['wrapper'], observed['files'])
        if anchor is None:
            anchor = current  # Boot/start-ticks/UID are in the authenticated wrapper identity.
        assert current == anchor, 'Startup anchor drift'
        members = {member['pid']: member for member in observed['members']}
        assert all(members.get(pid) == member for pid, member in seen.items()), 'Startup member drift'
        seen = members
        assert time.monotonic() < deadline, 'Native startup observation timed out'
        if observed['ready']:
            print('NATIVE STARTUP PIN ' + json.dumps({key: value for key, value in observed.items()
                if key != 'files'}, sort_keys=True), flush=True)
            return observed
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
    raise AssertionError('Native startup observation timed out without handoff; no installer retry')


def worker_abort_diagnostics(phase):
    """Read-only, finite generated-guest evidence; never infer a vanished PID's namespace."""
    assert sys.platform == 'linux' and os.geteuid() == 0
    require_lab_guest()
    unit = UNITS[1]
    def emit(kind, **values):
        print('NATIVE WORKER DIAGNOSTIC ' + json.dumps(dict(phase=phase, unit=unit, kind=kind,
            **values), sort_keys=True), flush=True)
    keys = ('InvocationID', 'ExecMainCode', 'ExecMainStatus', 'NRestarts',
            'ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic',
            'ActiveEnterTimestampMonotonic', 'StateChangeTimestampMonotonic',
            'ProtectSystem', 'ReadWritePaths', 'MainPID', 'ActiveState', 'SubState',
            'Result', 'ControlPID', 'Job')
    try:
        values = properties(unit, keys, timeout=5)
        emit('properties', values={key: values[key] for key in keys})
    except Exception as error:
        emit('properties', unavailable=type(error).__name__)
    # Exact unit/current boot is deliberately independent of missing/stale InvocationID.
    # Never broaden to the whole journal or attribute these lines to one invocation.
    try:
        journal = run('/usr/bin/journalctl', '--no-pager', '-b', '-u', unit,
                      '-n', '40', '-o', 'short-monotonic', check=False, timeout=5)
        emit('journal', scope='exact-unit-current-boot-not-single-invocation',
             returncode=journal.returncode, text=journal.stdout[-16384:])
    except Exception as error:
        emit('journal', unavailable=type(error).__name__)
    try:
        metadata = PING_FILE.parent.lstat()
        emit('ping-directory', scope='guest-root-namespace-not-service', path=str(PING_FILE.parent),
             metadata=dict(uid=metadata.st_uid, gid=metadata.st_gid, mode=oct(stat.S_IMODE(metadata.st_mode)),
                           nlink=metadata.st_nlink, is_directory=stat.S_ISDIR(metadata.st_mode),
                           is_symlink=stat.S_ISLNK(metadata.st_mode)))
        if stat.S_ISDIR(metadata.st_mode):
            mount = run('/usr/bin/findmnt', '--noheadings', '--output', 'TARGET,FSTYPE,OPTIONS',
                        '--target', str(PING_FILE.parent), check=False, timeout=5)
            emit('ping-mount', scope='guest-root-namespace-not-service',
                 returncode=mount.returncode, text=mount.stdout[-2048:])
    except Exception as error:
        emit('ping-filesystem', scope='guest-root-namespace-not-service', unavailable=type(error).__name__)


def characterize_worker_watchdog(bootstrap, config):
    """Administrative lab-only transitions; no business ACK or automatic rollback."""
    unit = UNITS[1]
    helper = override_path(unit).with_name('91-native-watchdog-admin.conf')
    phase = 'preconditions'
    try:
        lab_window(bootstrap, config)
        observed = watchdog_snapshot(bootstrap, config, None, '5min')
        prior_usec = watchdog_bus_property('WatchdogUSec')
        assert prior_usec == 300000000  # Exact recorded microseconds, not a rounded systemctl display.
        print('NATIVE WATCHDOG PRIOR ' + json.dumps(dict(unit=unit, recorded_usec=prior_usec)), flush=True)
        print('NATIVE WATCHDOG IDENTITY ' + json.dumps(observed, sort_keys=True), flush=True)
        old_usec, expected_watchdog = None, '5min'
        for phase, usec in (('disable-first', 0), ('restore-recorded', prior_usec), ('disable-final', 0)):
            lab_window(bootstrap, config)
            assert watchdog_snapshot(bootstrap, config, old_usec, expected_watchdog) == observed
            if old_usec is None:
                write_new(helper, watchdog_helper(usec))
            else:
                # Snapshot authenticated exact old bytes/mode/owner/nlink and safe ancestors.
                helper.write_text(watchdog_helper(usec))
            run('/usr/bin/systemctl', 'daemon-reload')
            assert watchdog_snapshot(bootstrap, config, usec, expected_watchdog) == observed
            lab_window(bootstrap, config)
            run('/usr/bin/systemctl', '--job-mode=fail', 'reload', unit)
            expected_watchdog = '0' if usec == 0 else '5min'
            assert watchdog_snapshot(bootstrap, config, usec, expected_watchdog) == observed
            print('NATIVE WATCHDOG TRANSITION ' + json.dumps(dict(phase=phase, unit=unit,
                requested_usec=usec, observed_watchdog=expected_watchdog, same_identity=True)), flush=True)
            if phase == 'disable-first':
                after_zero_ns = time.monotonic_ns()
                baseline = watchdog_pings(config)[-1]['sequence']
                def fresh_pings():
                    assert watchdog_snapshot(bootstrap, config, usec, '0') == observed
                    pings = watchdog_pings(config)
                    assert all(item['pid'] == observed['worker']['pid'] for item in pings)
                    return pings if (pings[0]['sequence'] > baseline and
                                    pings[0]['monotonic_ns'] > after_zero_ns) else None
                pings = until(fresh_pings, 'two real synthetic pings after first zero', seconds=10)
                assert watchdog_snapshot(bootstrap, config, usec, '0') == observed
                print('NATIVE WATCHDOG PINGS ' + json.dumps(dict(after_zero_ns=after_zero_ns,
                    baseline_sequence=baseline, pings=pings, effective_watchdog='0')), flush=True)
            old_usec = usec
        phase = 'remove-helper'
        lab_window(bootstrap, config)
        assert watchdog_snapshot(bootstrap, config, 0, '0') == observed
        helper.unlink()  # Only this distinct, exact authenticated helper; shutdown override remains.
        run('/usr/bin/systemctl', 'daemon-reload')
        assert watchdog_snapshot(bootstrap, config, None, '0') == observed
        print('NATIVE WATCHDOG HELPER REMOVED; effective zero and original identity verified', flush=True)
        return observed
    except Exception:
        # Never retry, roll back, or signal a workload/control process on unknown state.
        try:
            values = properties(unit, WATCHDOG_KEYS)
            selected = ('ActiveState', 'SubState', 'Result', 'MainPID', 'ControlPID', 'Job', 'WatchdogUSec')
            print('NATIVE WATCHDOG ABORT ' + json.dumps(dict(phase=phase, unit=unit,
                properties={key: values[key] for key in selected})), flush=True)
        except Exception:
            print('NATIVE WATCHDOG ABORT; finite metadata unavailable; phase=' + phase, flush=True)
        raise


def set_overrides(bootstrap, config, units):
    lab_window(bootstrap, config)
    worker_observed = None
    for unit in units:
        write_new(override_path(unit), OVERRIDE)
    run('/usr/bin/systemctl', 'daemon-reload')
    for unit in units:
        values = properties(unit)
        if unit == UNITS[1]:
            worker_observed = characterize_worker_watchdog(bootstrap, config)
            values = properties(unit)
        required = {
            'UnitFileState': 'disabled', 'Restart': 'no', 'WatchdogUSec': '0',
            'SendSIGKILL': 'no', 'TimeoutStopUSec': 'infinity', 'Job': '',
        }
        print('NATIVE OVERRIDE CHECK ' + json.dumps({
            'phase': 'set_overrides', 'unit': unit,
            'properties': {key: values[key] for key in required},
            'differing_keys': [key for key, expected in required.items() if values[key] != expected],
        }, sort_keys=True), flush=True)
        assert all(values[key] == expected for key, expected in required.items())
    return worker_observed


def remove_overrides(bootstrap, config, units):
    lab_window(bootstrap, config)
    for unit in units:
        path = override_path(unit)
        metadata = path.lstat()
        assert not path.is_symlink() and metadata.st_uid == 0 and metadata.st_nlink == 1
        assert path.read_text() == OVERRIDE
        path.unlink()  # Only the exact files created by this one owned guest run.
    run('/usr/bin/systemctl', 'daemon-reload')


def absence(bootstrap, config):
    for path in (config.control_dir, config.ack_dir, config.journal_root, config.bootstrap_root):
        bootstrap.absent(path)
        bootstrap.trusted_directory(config, path.parent)


EXECUTION_KEYS = ('ControlPID', 'NRestarts', 'InvocationID', 'ExecMainStartTimestampMonotonic',
                  'ActiveEnterTimestampMonotonic', 'StateChangeTimestampMonotonic')


def execution_files(bootstrap, config):
    paths = [config.systemd_dir / unit for unit in UNITS]
    paths.append(config.systemd_dir / (UNITS[1] + '.d/xvfb.conf'))
    files = {str(path): bootstrap.file_text(config, path) for path in paths}
    lock = config.global_lock.lstat()
    bootstrap.operator.metadata(lock, config.root_uid, config.root_gid, 0o600)
    return files, (lock.st_dev, lock.st_ino)


def execution_prior(bootstrap, config, unit, observed):
    """Finite pre-signal execution record, tied to every current group member."""
    values = properties(unit, bootstrap.PROPERTIES + EXECUTION_KEYS)
    main = observed['wrapper'] if unit == UNITS[0] else observed['worker']
    members = [identity(pid) for pid in group_pids(observed['group'])]
    assert len(members) == 3 and main in members
    if unit == UNITS[0]:
        assert observed['child'] in members
    else:
        assert sorted(members, key=lambda value: value['pid']) == sorted(
            observed['members'], key=lambda value: value['pid'])
    boot = bootstrap.audit.bounded_read(config.proc_root / 'sys/kernel/random/boot_id').strip()
    expected_uid = observed['child']['uid'] if unit == UNITS[0] else config.worker_uid
    assert all(member['boot_id'] == boot and member['uid'] == expected_uid and
               member['cgroup'] == '0::' + observed['group'] for member in members)
    assert all(values[key] == expected for key, expected in dict(
        ActiveState='active', SubState='running', Result='success', MainPID=str(main['pid']),
        ControlPID='0', NRestarts='0', Job='', ControlGroup=observed['group'],
        InvocationID=observed['invocation']).items())
    assert re.fullmatch('[0-9a-f]{32}', values['InvocationID'])
    assert re.fullmatch('[1-9][0-9]{0,9}', values['ExecMainPID'])
    assert int(values['ExecMainPID']) in {member['pid'] for member in members}
    if unit == UNITS[0]:
        assert values['ExecMainPID'] == str(observed['wrapper']['pid'])
    assert re.fullmatch('[1-9][0-9]{0,18}', values['ExecMainStartTimestampMonotonic'])
    with bootstrap.global_ex(config):
        files = execution_files(bootstrap, config)
    prior = dict(unit=unit, members=sorted(members, key=lambda value: value['pid']),
        invocation=values['InvocationID'], exec_main_pid=values['ExecMainPID'],
        exec_main_start=values['ExecMainStartTimestampMonotonic'], files=files)
    print('NATIVE PRE-SIGNAL EXECUTION ' + json.dumps(
        {key: value for key, value in prior.items() if key != 'files'}, sort_keys=True), flush=True)
    return prior


def final_execution_metadata(bootstrap, config, services, auxiliary, priors):
    """Validate observations, never fill/normalize the real installer's map."""
    assert set(services) == set(auxiliary) == set(priors) == set(UNITS)
    boot = bootstrap.audit.bounded_read(config.proc_root / 'sys/kernel/random/boot_id').strip()
    for unit in UNITS:
        values, prior = auxiliary[unit], priors[unit]
        assert set(values) == set(bootstrap.PROPERTIES + EXECUTION_KEYS)
        assert services[unit] == {key: values[key] for key in bootstrap.PROPERTIES}
        assert prior['unit'] == unit and len(prior['members']) == 3
        assert all(member['boot_id'] == boot for member in prior['members'])
        for member in prior['members']:
            bootstrap.absent(config.proc_root / str(member['pid']))  # Reuse also aborts.
        assert all(values[key] == expected for key, expected in dict(
            UnitFileState='disabled', MainPID='0', ControlPID='0', NRestarts='0',
            ControlGroup='', Job='').items())
        assert values['Slice'] in ('legaltech.slice', 'system.slice')
        if unit == UNITS[1]:
            assert (values['ActiveState'], values['SubState'], values['Result']) == ('inactive', 'dead', 'success')
            unavailable = dict(ExecMainCode='0', ExecMainStatus='0', ExecMainPID='0',
                ExecMainStartTimestampMonotonic='0', ExecMainExitTimestampMonotonic='0',
                ActiveEnterTimestampMonotonic='0', StateChangeTimestampMonotonic='0', InvocationID='')
            if all(values[key] == expected for key, expected in unavailable.items()):
                result = dict(worker_exit_metadata='execution-metadata-unavailable', worker_exit_status='unknown')
                continue
            result = dict(worker_exit_metadata='retained-clean-exit-record', worker_exit_status='0')
            assert values['ExecMainStatus'] == '0'
        else:
            assert (values['ActiveState'], values['SubState'], values['Result'], values['ExecMainStatus']) == (
                'failed', 'failed', 'exit-code', '143')
        assert values['ExecMainCode'] == '1' and values['ExecMainPID'] == prior['exec_main_pid']
        assert values['ExecMainStartTimestampMonotonic'] == prior['exec_main_start']
        assert values['InvocationID'] == prior['invocation']
        for key in ('ExecMainPID', 'ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic',
                    'ActiveEnterTimestampMonotonic', 'StateChangeTimestampMonotonic'):
            assert re.fullmatch('[1-9][0-9]{0,18}', values[key])
        assert int(values['ExecMainExitTimestampMonotonic']) >= int(values['ExecMainStartTimestampMonotonic'])
    bootstrap.empty_cgroups(config)
    assert bootstrap.audit.bounded_read(config.proc_root / 'sys/kernel/random/boot_id').strip() == boot
    return result


def verify_snapshot_state_rejection(bootstrap, error, *, stopped=None):
    """Observe the real exception's exact frozen state gate; never alter it."""
    source_path = Path(bootstrap.__file__)
    source = source_path.read_bytes()
    assert hashlib.sha256(source).hexdigest() == INSTALLER_SHA256, 'Installer source drift requires review'
    snapshot = bootstrap.stopped_snapshot
    assert type(snapshot) is FunctionType
    assert snapshot.__code__.co_filename == str(source_path)
    # Compile without executing: bind the loaded function to the pinned source,
    # so a replacement function cannot authenticate its own unrelated traceback.
    compiled = compile(source, str(source_path), 'exec', dont_inherit=True)
    expected = [item for item in compiled.co_consts
                if isinstance(item, CodeType) and item.co_name == 'stopped_snapshot']
    assert len(expected) == 1 and snapshot.__code__ == expected[0], 'Snapshot code is not the frozen function'
    frames = []
    cursor = error.__traceback__
    while cursor is not None:
        if cursor.tb_frame.f_code is snapshot.__code__:
            frames.append(cursor)
        cursor = cursor.tb_next
    assert len(frames) == 1 and frames[0].tb_lineno == STOPPED_UNIT_STATE_LINE, 'Wrong snapshot rejection gate'
    terminal = frames[0].tb_next
    assert terminal is not None and terminal.tb_frame.f_code is bootstrap.require.__code__
    assert terminal.tb_next is None, 'Nested prerequisite failure is not a state predicate rejection'
    if stopped is not None:
        config, services, auxiliary, priors = stopped
        actual = frames[0].tb_frame.f_locals
        assert actual['config'] is config and actual['services'] == services
        assert actual['values'] is actual['services'][UNITS[0]], 'Rejection was not the API iteration'
        final_execution_metadata(bootstrap, config, actual['services'], auxiliary, priors)


def prove_rejection(bootstrap, config, *, active, priors=None, startup=None):
    """Validate prerequisites independently; reject at the real snapshot gate."""
    with bootstrap.global_ex(config):
        lab_window(bootstrap, config)
        bootstrap.exact_tree(config, subprocess.run)
        absence(bootstrap, config)
        services = bootstrap.installed_files(config, subprocess.run)
        bootstrap.target_unit(bootstrap.file_text(config, config.systemd_dir / UNITS[1]))
        assert all(value['UnitFileState'] == 'disabled' for value in services.values())
        if active:
            assert all(value['ActiveState'] == 'active' for value in services.values())
            assert startup is not None and startup['ready']
            assert startup_worker_snapshot(bootstrap, config) == startup, 'Post-pin startup drift'
        else:
            assert priors is not None
            assert all(prior['files'] == execution_files(bootstrap, config) for prior in priors.values())
            auxiliary = {unit: properties(unit, bootstrap.PROPERTIES + EXECUTION_KEYS) for unit in UNITS}
            worker_metadata = final_execution_metadata(bootstrap, config, services, auxiliary, priors)
        try:
            bootstrap.stopped_snapshot(config, subprocess.run)
        except bootstrap.MaintenanceError as error:
            verify_snapshot_state_rejection(bootstrap, error,
                stopped=None if active else (config, services, auxiliary, priors))
        else:
            raise AssertionError('Installer snapshot unexpectedly accepted characterization state')
        if not active:
            assert auxiliary == {unit: properties(unit, bootstrap.PROPERTIES + EXECUTION_KEYS) for unit in UNITS}
            assert all(prior['files'] == execution_files(bootstrap, config) for prior in priors.values())
        else:
            assert startup_worker_snapshot(bootstrap, config) == startup, 'Post-pin startup drift'
    # Do not retain EX while calling the real CLI, which acquires its own lock.
    paths = [config.systemd_dir / unit for unit in UNITS]
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    lock_inode = config.global_lock.stat().st_ino
    result = run('/usr/bin/python3', '-B', str(REPO / 'ops/bootstrap-worker-maintenance.py'),
                 'install', '--expected-sha', config.expected_sha, check=False)
    assert result.returncode == 1 and json.loads(result.stdout) == {
        'operation_id': None, 'phase': 'validation', 'result': 'blocked',
    }, 'CLI did not reject cleanly before any initial control publication'
    with bootstrap.global_ex(config):
        lab_window(bootstrap, config)
        bootstrap.exact_tree(config, subprocess.run)
        absence(bootstrap, config)
        installed_after = bootstrap.installed_files(config, subprocess.run)
        if installed_after != services:
            for unit in UNITS:
                differing = [key for key in bootstrap.PROPERTIES if services[unit][key] != installed_after[unit][key]]
                if differing:
                    print('NATIVE INSTALLED SNAPSHOT MISMATCH ' + json.dumps(dict(
                        phase='active-legacy' if active else 'stopped-characterization', unit=unit,
                        differing_keys=differing, before={key: services[unit][key] for key in differing},
                        after={key: installed_after[unit][key] for key in differing}), sort_keys=True), flush=True)
        assert installed_after == services
        if active:
            assert startup_worker_snapshot(bootstrap, config) == startup, 'Post-pin startup drift'
        if not active:
            after = {unit: properties(unit, bootstrap.PROPERTIES + EXECUTION_KEYS) for unit in UNITS}
            assert after == auxiliary
            assert final_execution_metadata(bootstrap, config, services, after, priors) == worker_metadata
            assert all(prior['files'] == execution_files(bootstrap, config) for prior in priors.values())
    assert before == [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    assert config.global_lock.stat().st_ino == lock_inode
    if not active:
        print('NATIVE WORKER EXECUTION METADATA ' + json.dumps(worker_metadata, sort_keys=True), flush=True)
    print('NATIVE PRODUCTION CLI BLOCKED ' + json.dumps(cli_rejection_evidence(), sort_keys=True), flush=True)
    print('NATIVE INSTALLER REJECTED REAL ' + ('ACTIVE LEGACY' if active else '143 SNAPSHOT') +
          '; DIRECT snapshot state gate authenticated; CLI blocked separately, gate not exposed; '
          'prerequisites revalidated; no control/ACK/journal/bootstrap/unit/lifecycle mutation', flush=True)


def prepare_runtime_storage():
    write_new(REPO / '.gitignore', 'estrado-pjud-service/.venv\n'
              '/estrado-pjud-service/logs/native-bootstrap-watchdog/\n')
    for directory, mode in ((APP / 'logs', 0o755), (PING_FILE.parent, 0o700)):
        directory.mkdir(mode=mode)
        os.chown(directory, pwd.getpwnam('estrado').pw_uid, grp.getgrnam('estrado').gr_gid)


def setup(bootstrap):
    assert not REPO.exists() and not LAB.exists()
    assert not Path('/run/lock/legaltech-resource-guards.lock').exists()
    for unit in UNITS:
        assert not (Path('/etc/systemd/system') / unit).exists()
        assert not override_path(unit).exists()
    run('/usr/bin/python3', '-c', 'import sys; assert sys.version_info[:2] == (3,12)')
    run('/opt/native-runtime/bin/python', '-c',
        "import importlib.metadata as m; assert {n:m.version(n) for n in ('uvicorn','click','h11')} == "
        "{'uvicorn':'0.41.0','click':'8.1.8','h11':'0.16.0'}")
    run('/usr/sbin/useradd', '--system', '--create-home', 'estrado')
    REPO.mkdir(); LAB.mkdir()
    shutil.copytree('/mnt/payload/ops', REPO / 'ops')
    (APP / 'worker').mkdir(parents=True)
    for name in ('__init__.py', 'maintenance.py', 'maintenance_store.py', 'sd_notify.py', 'maintenance_heartbeat.py'):
        shutil.copy2(Path('/mnt/payload/estrado-pjud-service/worker') / name, APP / 'worker' / name)
    (APP / '.venv').symlink_to('/opt/native-runtime', target_is_directory=True)
    prepare_runtime_storage()
    write_new(APP / 'app/__init__.py', '')
    write_new(APP / 'app/main.py', asgi_source(MODE))
    write_new(APP / 'worker/__main__.py', legacy_source(PING_FILE))
    write_new(MODE, 'normal')
    write_new(Path('/etc/systemd/system/legaltech.slice'), '[Slice]\nCPUWeight=1000\n')
    for unit, body in synthetic_units(REPO / 'ops').items():
        write_new(Path('/etc/systemd/system') / unit, body)
    drop = Path('/etc/systemd/system/estrado-pjud-worker.service.d/xvfb.conf')
    write_new(drop, (REPO / 'ops/systemd/estrado-pjud-worker.service.d/xvfb.conf').read_text())
    write_new(Path('/run/lock/legaltech-resource-guards.lock'), '', 0o600)
    run('/usr/bin/git', '-C', str(REPO), 'init', '-q')
    run('/usr/bin/git', '-C', str(REPO), 'add', '.')
    run('/usr/bin/git', '-C', str(REPO), '-c', 'user.name=Native Characterization',
        '-c', 'user.email=native@example.invalid', 'commit', '-qm', 'synthetic guest payload only')
    sha = run('/usr/bin/git', '-C', str(REPO), 'rev-parse', 'HEAD').stdout.strip()
    config = bootstrap.Config(sha, worker_uid=pwd.getpwnam('estrado').pw_uid,
                              worker_gid=grp.getgrnam('estrado').gr_gid)
    absence(bootstrap, config)
    run('/usr/bin/systemctl', 'daemon-reload')
    return config


def characterize():
    assert 'VERSION_ID="24.04"' in Path('/etc/os-release').read_text()
    assert Path('/sys/fs/cgroup/cgroup.controllers').is_file()
    assert hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal')
    bootstrap = load_bootstrap(Path('/mnt/payload/ops/bootstrap-worker-maintenance.py'))
    lab_window(bootstrap, bootstrap.Config('a' * 40))  # Real clock; never adjusted by this lab.
    print('NATIVE SHUTDOWN CHARACTERIZATION ONLY; NO BUSINESS/DRAIN/BOOTSTRAP PROOF', flush=True)
    print('NATIVE LAB CLOCK ' + json.dumps(dict(allow_daytime_lab=ALLOW_DAYTIME_LAB,
        actual_santiago=datetime.now(timezone.utc).astimezone(ZoneInfo('America/Santiago')).isoformat())), flush=True)
    print(run('/usr/bin/systemctl', '--version').stdout.splitlines()[0], flush=True)
    config = setup(bootstrap)
    lab_window(bootstrap, config)
    run('/usr/bin/systemctl', 'start', *UNITS)
    assert all(properties(unit)['UnitFileState'] == 'disabled' for unit in UNITS)
    startup = observe_worker_startup(bootstrap, config)
    prove_rejection(bootstrap, config, active=True, startup=startup)
    priors = {}
    for case in ('normal', 'lifespan_error'):
        if case != 'normal':
            lab_window(bootstrap, config)
            MODE.write_text(case)  # Only this generated guest-local case selector.
            run('/usr/bin/systemctl', 'start', UNITS[0])
        changed = UNITS if case == 'normal' else (UNITS[0],)
        worker_admin = set_overrides(bootstrap, config, changed)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def healthy():
            try:
                with opener.open('http://127.0.0.1:8000/api/v1/health', timeout=1) as response:
                    return response.status == 200
            except OSError:
                return False
        until(healthy, 'synthetic API readiness')
        observed = api_identity()
        print('NATIVE API IDENTITY ' + json.dumps(observed, sort_keys=True), flush=True)
        assert api_identity() == observed
        priors[UNITS[0]] = execution_prior(bootstrap, config, UNITS[0], observed)
        if case == 'normal':
            assert watchdog_snapshot(bootstrap, config, None, '0') == worker_admin
            priors[UNITS[1]] = execution_prior(bootstrap, config, UNITS[1], worker_admin)
        signal_once(bootstrap, config, observed['child'])
        def api_exited():
            values = properties(UNITS[0])
            return values if values['MainPID'] == '0' and not group_pids(observed['group']) else None
        final = until(api_exited, 'API child/wrapper and cgroup empty')
        assert not Path('/proc', str(observed['child']['pid'])).exists()
        assert not Path('/proc', str(observed['wrapper']['pid'])).exists()
        journal = run('/usr/bin/journalctl', '--no-pager', '-o', 'cat',
                      '_SYSTEMD_INVOCATION_ID=' + observed['invocation']).stdout
        print('NATIVE API FINAL ' + json.dumps(final, sort_keys=True), flush=True)
        print('NATIVE SYNTHETIC JOURNAL ' + case + '\n' + journal[-12000:], flush=True)
        evidence = shutdown_evidence(case, final, journal)
        if case == 'normal':
            assert watchdog_snapshot(bootstrap, config, None, '0') == worker_admin
            worker = properties(UNITS[1])
            worker_pid = int(worker['MainPID'])
            worker_id = identity(worker_pid)
            assert worker_id == worker_admin['worker']
            assert worker_id['uid'] == config.worker_uid
            assert worker_id['cgroup'] == '0::' + worker['ControlGroup']
            assert commandline(worker_pid)[-2:] == [b'-m', b'worker']
            assert b'/usr/bin/xvfb-run' in commandline(worker_id['parent_pid'])
            print('NATIVE LEGACY WORKER IDENTITY ' + json.dumps(worker_id, sort_keys=True), flush=True)
            assert execution_prior(bootstrap, config, UNITS[1], worker_admin) == priors[UNITS[1]]
            signal_once(bootstrap, config, worker_id)
            until(lambda: properties(UNITS[1])['MainPID'] == '0' and not group_pids(worker['ControlGroup']),
                  'legacy worker/wrapper and cgroup empty')
        print('NATIVE WORKER FINAL ' + json.dumps(properties(UNITS[1]), sort_keys=True), flush=True)
        bootstrap.empty_cgroups(config)
        remove_overrides(bootstrap, config, changed)
        prove_rejection(bootstrap, config, active=False, priors=priors)
        print('NATIVE CHARACTERIZATION CASE ' + json.dumps(evidence, sort_keys=True), flush=True)
    print('NATIVE CHARACTERIZATION COMPLETE: both exits143, different lifespan outcomes; '
          'unchanged installer rejects; BOOTSTRAP TASK INCOMPLETE', flush=True)


def main(*, allow_daytime_lab=False):
    global ALLOW_DAYTIME_LAB
    assert type(allow_daytime_lab) is bool
    ALLOW_DAYTIME_LAB = allow_daytime_lab
    require_lab_guest()  # Outside the failure collector: never diagnose an unverified host.
    try:
        characterize()
    except Exception:
        try:
            worker_abort_diagnostics('characterization')
        except Exception as diagnostic_error:
            print('NATIVE WORKER DIAGNOSTIC unavailable: ' + type(diagnostic_error).__name__, flush=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-daytime-lab', action='store_true')
    main(allow_daytime_lab=parser.parse_args().allow_daytime_lab)
