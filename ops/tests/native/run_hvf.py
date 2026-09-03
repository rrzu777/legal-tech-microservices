"""Disposable macOS HVF laboratory. No VPS access, shared directories or real secrets."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
QEMU = '/opt/homebrew/opt/qemu/bin/qemu-system-aarch64'
BASE_HASH = 'afa139bac6f2629c1e1f2f8f34215f3a9ad9779801bcb945521ba1a45016743f'
CANCELLED = False
WORKER_PAYLOAD = tuple('estrado-pjud-service/worker/' + name for name in
                       ('__init__.py', 'maintenance.py', 'maintenance_store.py', 'sd_notify.py',
                        'maintenance_heartbeat.py'))
BOOTSTRAP_PAYLOAD = ('ops/tests/native/bootstrap_exercise.py', 'ops/bootstrap-worker-maintenance.py',
                     'ops/bootstrap-audit.py', 'ops/worker-maintenance.py')
BOOTSTRAP_REQUIREMENTS = (
    'uvicorn==0.41.0 --hash=sha256:29e35b1d2c36a04b9e180d4007ede3bcb32a85fbdfd6c6aeb3f26839de088187\n'
    'click==8.1.8 --hash=sha256:63c132bbbed01578a06712a2d1f497bb62d9c1c0d329b7903a866228027263b2\n'
    'h11==0.16.0 --hash=sha256:63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86\n'
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-image', type=Path, required=True)
    parser.add_argument('--mode', choices=('integral', 'bootstrap-characterization'), default='integral')
    parser.add_argument('--allow-daytime-lab', action='store_true',
                        help='Allow daytime lifecycle characterization only in the disposable guest')
    args = parser.parse_args(argv)
    if args.allow_daytime_lab and args.mode != 'bootstrap-characterization':
        parser.error('--allow-daytime-lab requires --mode bootstrap-characterization')
    return args


def native_stages(mode):
    if mode == 'integral':
        return ('probe.py', 'fixture.py', 'exercise.py')
    if mode == 'bootstrap-characterization':
        return ('bootstrap_exercise.py',)
    raise ValueError('Unknown native mode')


def stage_command(script, *, allow_daytime_lab=False):
    if script not in (*native_stages('integral'), *native_stages('bootstrap-characterization')):
        raise ValueError('Unknown native stage')
    if allow_daytime_lab and script != 'bootstrap_exercise.py':
        raise ValueError('Daytime opt-in is exclusive to shutdown characterization')
    command = 'sudo python3 -u /mnt/payload/ops/tests/native/' + script
    if script == 'bootstrap_exercise.py':
        command = ('sudo env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 '
                   '/usr/bin/python3 -B -u /mnt/payload/ops/tests/native/' + script)
        if allow_daytime_lab:
            command += ' --allow-daytime-lab'
    return command


def cloud_config(public_key, *, mode='integral'):
    native_stages(mode)  # Validate before creating any guest setup configuration.
    config = {
        'ssh_pwauth': False, 'ssh_genkeytypes': ['ed25519'], 'disable_root': True,
        'ssh_authorized_keys': [public_key], 'package_update': True,
        'packages': ['git', 'jq', 'curl', 'cron', 'xvfb', 'xauth', 'dbus-user-session'],
        'mounts': [['LABEL=PAYLOAD', '/mnt/payload', 'iso9660', 'ro,nofail', '0', '0']],
        'bootcmd': [['sh', '-c', 'test ! -f /etc/update-motd.d/90-updates-available || chmod a-x /etc/update-motd.d/90-updates-available']],
        'runcmd': [['sh', '-ec', 'for t in git jq curl xvfb-run xauth busctl; do command -v "$t"; done; mountpoint -q /mnt/payload; touch /var/lib/native-ready']],
    }
    if mode == 'bootstrap-characterization':
        config['packages'].append('python3-venv')
        config['write_files'] = [{
            'path': '/opt/native-runtime-requirements.txt', 'owner': 'root:root', 'permissions': '0644',
            'content': BOOTSTRAP_REQUIREMENTS,
        }]
        # The final marker verifies actual installed distributions because
        # cloud-init may continue runcmd after a prior command fails.
        check = ('/opt/native-runtime/bin/python -c "import importlib.metadata as m; '
                 "assert {k:m.version(k) for k in ('uvicorn','click','h11')} == "
                 "{'uvicorn':'0.41.0','click':'8.1.8','h11':'0.16.0'}" + '"; ')
        ready = config['runcmd'][0][-1].replace('touch /var/lib/native-ready', check + 'touch /var/lib/native-ready')
        config['runcmd'] = [
            ['/usr/bin/python3', '-m', 'venv', '/opt/native-runtime'],
            ['/opt/native-runtime/bin/python', '-m', 'pip', '--isolated', 'install',
             '--require-hashes', '--only-binary=:all:', '--no-deps', '--disable-pip-version-check',
             '--no-cache-dir', '--retries', '0', '--timeout', '20', '--index-url', 'https://pypi.org/simple',
             '-r', '/opt/native-runtime-requirements.txt'],
            ['sh', '-ec', ready],
        ]
    return config


def run(*args, check=True, timeout=60):
    check_cancelled()
    result = subprocess.run(args, check=check, timeout=timeout, text=True, capture_output=True)
    check_cancelled()
    return result


def validate_payload(root, paths):
    for relative in paths:
        path = Path(relative)
        if (not path.parts or path.is_absolute() or str(path) != relative or '..' in path.parts
                or any(part.startswith('.env') for part in path.parts)
                or (path.parts[0] != 'ops' and relative not in WORKER_PAYLOAD)):
            raise RuntimeError('Unsafe native payload path')
        for parent in (path, *path.parents):
            if (root / parent).is_symlink():
                raise RuntimeError('Linked native payload path')
        if not (root / path).is_file():
            raise RuntimeError('Missing native payload file')


def payload_files(root, *, mode='integral'):
    native_stages(mode)
    files = list(filter(None, run('git', '-C', str(root), 'ls-files', '-z', 'ops').stdout.split('\0')))
    files += ['ops/tests/native/' + name for name in
              ('fixture.py', 'fixture_worker.py', 'exercise.py', 'probe.py')]
    files += list(WORKER_PAYLOAD)
    if mode == 'bootstrap-characterization':
        files += list(BOOTSTRAP_PAYLOAD)
    validate_payload(root, files)
    return sorted(set(files))


def clean_workspace(work, owner):
    if work.is_symlink() or not work.name.startswith('resource-guards-hvf-'):
        raise RuntimeError('Unsafe cleanup directory')
    marker = work / 'owner'
    if not marker.is_file() or marker.is_symlink() or marker.read_text() != owner:
        raise RuntimeError('Refusing cleanup of unowned workspace')
    shutil.rmtree(work)


def make_payload_media(payload, media):
    manifest = {}
    with tarfile.open(media / 'payload.tar', 'w') as archive:
        for path in sorted(payload.rglob('*')):
            if not path.is_file():
                continue
            relative = str(path.relative_to(payload))
            archive.add(path, arcname=relative, recursive=False)
            manifest[relative] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                  'mode': path.stat().st_mode & 0o777}
    body = json.dumps(manifest, sort_keys=True).encode()
    (media / 'manifest.json').write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def qemu_arguments(work, port, *, isolated):
    return [QEMU, '-machine', 'virt', '-cpu', 'host', '-accel', 'hvf',
            '-smp', '2', '-m', '4096',
            '-bios', '/opt/homebrew/opt/qemu/share/qemu/edk2-aarch64-code.fd',
            '-drive', f'if=virtio,format=qcow2,file={work}/guest.qcow2',
            '-drive', f'if=virtio,format=raw,file={work}/seed.iso,readonly=on',
            '-drive', f'if=virtio,format=raw,file={work}/payload.iso,readonly=on',
            '-netdev', 'user,id=net0,restrict=' + ('on' if isolated else 'off') +
            f',ipv6=off,hostfwd=tcp:127.0.0.1:{port}-:22',
            '-device', 'virtio-net-device,netdev=net0',
            '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot']


def pressure_ok(minimum):
    output = run('/usr/bin/memory_pressure', timeout=10).stdout
    found = re.search(r'System-wide memory free percentage: (\d+)%', output)
    return found is not None and int(found[1]) >= minimum


def watch_guest(process, stop):
    while process.poll() is None and not stop.is_set():
        try:
            healthy = pressure_ok(12)
        except Exception:
            healthy = False
        if not healthy:
            process.terminate()
            print('Host pressure watchdog stopped the owned VM', flush=True)
            return
        stop.wait(10)


def small_guest_setup():
    body = '#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = -b ] || exit 2\nprintf "              total used free shared buff/cache available\\nMem: 8589934592 1073741824 7516192768 0 0 7516192768\\n"\n'
    return f'''sudo python3 - <<'PY'
import json
from pathlib import Path
p=Path('/opt/native-fixture/free-admission')
p.write_text({body!r})
p.chmod(0o755)
e=Path('/opt/native-fixture/environment.json')
v=json.loads(e.read_text()); v['RG_FREE_BIN']=str(p); e.write_text(json.dumps(v))
PY'''


def terminate_runner(signum, frame):
    # Never throw between child creation and assignment of its Popen handle.
    global CANCELLED
    CANCELLED = True


def check_cancelled():
    if CANCELLED:
        raise InterruptedError('HVF runner interrupted; cleaning owned guest')


def main():
    args = parse_args()
    if platform.system() != 'Darwin' or platform.machine() != 'arm64' or os.geteuid() == 0:
        raise SystemExit('Requires unprivileged macOS ARM64')
    if not pressure_ok(30) or shutil.disk_usage('/private/tmp').free < 40 * 1024**3:
        raise SystemExit('Insufficient host headroom for bounded HVF test')
    base = args.base_image.resolve(strict=True)
    with base.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != BASE_HASH:
            raise SystemExit('Ubuntu base image checksum mismatch')
    print(run(QEMU, '--version').stdout.splitlines()[0], flush=True)
    if args.mode == 'integral':
        print('HVF guest: 2 CPUs / 4 GiB; RAM admission is a fixture, NOT capacity proof', flush=True)
    else:
        print('HVF guest: 2 CPUs / 4 GiB; shutdown characterization only, NOT capacity proof', flush=True)
    work = Path(tempfile.mkdtemp(prefix='resource-guards-hvf-', dir='/private/tmp'))
    evidence = Path(tempfile.mkdtemp(prefix='resource-guards-hvf-evidence-', dir='/private/tmp'))
    owner = uuid.uuid4().hex
    (work / 'owner').write_text(owner)
    print('Evidence: ' + str(evidence), flush=True)
    process = None
    watchers = []
    canary = socket.socket()
    try:
        canary.bind(('127.0.0.1', 0)); canary.listen(4)
        canary_port = canary.getsockname()[1]
        payload, seed, media = work / 'payload', work / 'seed', work / 'media'
        payload.mkdir(); seed.mkdir(); media.mkdir()
        files = payload_files(ROOT, mode=args.mode)
        for relative in set(files):
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        manifest_hash = make_payload_media(payload, media)
        archive_hash = hashlib.sha256((media / 'payload.tar').read_bytes()).hexdigest()
        run('/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(work / 'id_ed25519'))
        config = cloud_config((work / 'id_ed25519.pub').read_text().strip(), mode=args.mode)
        (seed / 'user-data').write_text('#cloud-config\n' + json.dumps(config))
        (seed / 'meta-data').write_text('instance-id: ' + owner + '\nlocal-hostname: native-guards\n')
        for folder, label, filename in ((seed, 'cidata', 'seed.iso'), (media, 'PAYLOAD', 'payload.iso')):
            run('/usr/bin/hdiutil', 'makehybrid', '-iso', '-joliet', '-default-volume-name', label,
                '-o', str(work / filename), str(folder))
        run('/opt/homebrew/opt/qemu/bin/qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2',
            '-b', str(base), str(work / 'guest.qcow2'), '24G')
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        ssh = ['/usr/bin/ssh', '-F', '/dev/null', '-i', str(work / 'id_ed25519'), '-p', str(port),
               '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'ForwardAgent=no',
               '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=accept-new',
               '-o', 'UserKnownHostsFile=' + str(work / 'known_hosts'), 'ubuntu@127.0.0.1']
        for isolated in (False, True):
            phase = 'isolated' if isolated else 'bootstrap'
            with (evidence / (phase + '.log')).open('w') as log:
                # External supervisor: QEMU can override in-process SIGALRM.
                process = subprocess.Popen([sys.executable, str(HERE / 'supervisor.py'),
                                            '--seconds', '1800',
                                            *qemu_arguments(work, port, isolated=isolated)],
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                check_cancelled()
                stop_watch = threading.Event()
                watcher = threading.Thread(target=watch_guest, args=(process, stop_watch), daemon=True)
                watcher.start(); watchers.append((stop_watch, watcher))
                deadline = time.monotonic() + (180 if isolated else 900)
                while True:
                    if process.poll() is not None or time.monotonic() > deadline or not pressure_ok(12):
                        raise RuntimeError('HVF guest exited, timed out or host memory headroom exhausted')
                    try:
                        result = run(*ssh, 'test -e /var/lib/native-ready && mountpoint -q /mnt/payload', check=False, timeout=8)
                        if result.returncode == 0:
                            break
                    except subprocess.TimeoutExpired:
                        pass
                    time.sleep(3)
                if not isolated:
                    run(*ssh, f"python3 -c \"import socket; socket.create_connection(('10.0.2.2',{canary_port}),2).close()\"")
                    run(*ssh, 'sudo poweroff', check=False)
                    process.wait(timeout=60)
                    process = None
                    print('Bootstrap complete; rebooting with restricted networking', flush=True)
                    continue
                # A real socket attempt must fail, including access to host gateway.
                run(*ssh, f"python3 -c \"import socket; s=socket.socket(); s.settimeout(2); assert s.connect_ex(('10.0.2.2',{canary_port})) != 0\"")
                (evidence / 'isolation.log').write_text('Restricted QEMU user networking; host TCP probe refused\n')
                # ISO normalizes multi-dot/case-sensitive filenames on Linux.
                # Preserve them inside TAR, then bind the verified guest copy RO.
                unpack = f'''sudo python3 - <<'PY'
from pathlib import Path
import hashlib,json,stat,subprocess,tarfile
media=Path('/mnt/payload'); destination=Path('/opt/native-payload')
body=(media/'manifest.json').read_bytes()
assert hashlib.sha256(body).hexdigest() == {manifest_hash!r}
manifest=json.loads(body)
assert hashlib.sha256((media/'payload.tar').read_bytes()).hexdigest() == {archive_hash!r}
destination.mkdir()
with tarfile.open(media/'payload.tar') as archive:
    members=archive.getmembers()
    assert len(members) == len(manifest) and {{m.name for m in members}} == set(manifest)
    assert all(m.isfile() and not Path(m.name).is_absolute() and '..' not in Path(m.name).parts
               and not any(p.startswith('.env') for p in Path(m.name).parts)
               and (m.name.startswith('ops/') or m.name in {WORKER_PAYLOAD!r}) for m in members)
    archive.extractall(destination,filter='data')
for relative, expected in manifest.items():
    path=destination/relative
    assert path.is_file() and not path.is_symlink()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected['sha256']
    assert stat.S_IMODE(path.stat().st_mode) == expected['mode']
subprocess.run(['mount','--bind',str(destination),str(media)],check=True)
subprocess.run(['mount','-o','remount,bind,ro',str(media)],check=True)
print('NATIVE payload exact: '+str(len(manifest))+' files')
PY'''
                unpacked = run(*ssh, unpack)
                print(unpacked.stdout, flush=True)
                (evidence / 'payload.log').write_text(unpacked.stdout + 'manifest-sha256=' + manifest_hash + '\n')
                for script in native_stages(args.mode):
                    integral = script == 'exercise.py'
                    characterization = script == 'bootstrap_exercise.py'
                    if integral:
                        # Only the existing integral path substitutes free(1).
                        run(*ssh, small_guest_setup())
                    command = stage_command(script, allow_daytime_lab=args.allow_daytime_lab)
                    result = run(*ssh, command, check=False, timeout=900 if integral or characterization else 180)
                    log_name = 'integral.log' if integral else ('bootstrap-characterization.log' if characterization else script + '.log')
                    (evidence / log_name).write_text(result.stdout + result.stderr)
                    print(result.stdout + result.stderr, flush=True)
                    if result.returncode:
                        raise RuntimeError('Native stage failed: ' + script + '; no automatic retry')
                if args.mode == 'integral':
                    print('HVF INTEGRAL PASS (capacity admission excluded)', flush=True)
                else:
                    print('HVF SHUTDOWN CHARACTERIZATION ONLY; BOOTSTRAP TASK INCOMPLETE', flush=True)
    finally:
        for stop_watch, watcher in watchers:
            stop_watch.set(); watcher.join(timeout=12)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=10)
        canary.close()
        clean_workspace(work, owner)
        print('Removed owned VM disk, seed and ephemeral key; evidence retained', flush=True)


if __name__ == '__main__':
    for termination in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(termination, terminate_runner)
    main()
