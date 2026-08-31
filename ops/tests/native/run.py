"""Run native probes inside a resource-bounded, network-disconnected QEMU VM."""
from pathlib import Path
import argparse
import json
import os
import platform
import subprocess
import sys
import time
import uuid

IMAGE = 'resource-guards-native-vm:20260830'
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DOCKER = ['docker']
sys.path.insert(0, str(HERE))
from run_hvf import payload_files


def execute(*args, check=True, timeout=60):
    return subprocess.run(args, check=check, text=True, capture_output=True, timeout=timeout)


def local_docker():
    if platform.system() != 'Darwin' or any(os.environ.get(name) for name in
            ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')):
        raise RuntimeError('Requires local macOS Docker Desktop, without Docker endpoint overrides')
    context = json.loads(execute('docker', 'context', 'inspect').stdout)[0]
    endpoint = context['Endpoints']['docker']['Host']
    allowed = {'unix:///var/run/docker.sock', 'unix://' + str(Path.home() / '.docker/run/docker.sock')}
    if endpoint not in allowed:
        raise RuntimeError('Refusing non-local Docker endpoint')
    # Pin the inspected endpoint: changing the active context cannot redirect us.
    command = ['docker', '--host', endpoint]
    info = json.loads(execute(*command, 'info', '--format', '{{json .}}').stdout)
    if info['OperatingSystem'] != 'Docker Desktop' or info['Architecture'] != 'aarch64':
        raise RuntimeError('Requires ARM64 Docker Desktop')
    return command


def cleanup(name, owner):
    try:
        result = execute(*DOCKER, 'inspect', name, check=False)
        if result.returncode != 0:
            raise RuntimeError('Unable to verify container cleanup target')
        info = json.loads(result.stdout)[0]
        if info['Config']['Labels'].get('native-validation-owner') != owner:
            raise RuntimeError('Refusing cleanup of container not owned by this run')
        try:
            execute(*DOCKER, 'stop', '--time', '5', name, check=False, timeout=20)
        finally:
            # Force removal only of the exact container with our random owner label.
            execute(*DOCKER, 'rm', '--force', name, timeout=30)
    except Exception as error:
        print(f'Cleanup incomplete for {name}: {type(error).__name__}', file=sys.stderr)
        return False
    return True


def main():
    global DOCKER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true', help='build the pinned Ubuntu/QEMU image')
    parser.add_argument('--integral', action='store_true', help='also exercise real apply and both rollback paths')
    args = parser.parse_args()
    DOCKER = local_docker()
    if args.build:
        subprocess.run([*DOCKER, 'build', '-t', IMAGE, str(HERE)], check=True, timeout=1200)
    image_id = json.loads(execute(*DOCKER, 'image', 'inspect', IMAGE).stdout)[0]['Id']
    print('Native VM image: ' + image_id, flush=True)
    owner = uuid.uuid4().hex
    name = 'resource-guards-native-' + owner[:12]
    try:
        execute(*DOCKER, 'create', '--name', name,
                '--label', 'purpose=resource-guards-isolated-validation',
                '--label', 'native-validation-owner=' + owner,
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--memory', '3g', '--memory-swap', '3g', '--cpus', '2', '--pids-limit', '128',
                '--entrypoint', '/usr/bin/timeout', image_id,
                '3600', 'python3', '/usr/local/bin/native-vm-boot.py')
        # Tracked ops + explicit fixtures + five stdlib worker modules only.
        # Never copy the workspace,
        # .env files, SSH agent, Docker socket or a host filesystem mount.
        listing = '\0'.join(payload_files(ROOT)) + '\0'
        archive = subprocess.run(['tar', '-C', str(ROOT), '--null', '-T', '-', '-cf', '-'],
                                 input=listing.encode(), capture_output=True, check=True, timeout=60)
        subprocess.run([*DOCKER, 'cp', '-', name + ':/payload'], input=archive.stdout,
                       capture_output=True, check=True, timeout=60)
        execute(*DOCKER, 'cp', str(HERE / 'probe.py'), name + ':/payload/probe.py')
        if args.integral:
            for fixture in ('fixture.py', 'exercise.py'):
                execute(*DOCKER, 'cp', str(HERE / fixture), name + ':/payload/' + fixture)
        info = json.loads(execute(*DOCKER, 'inspect', name).stdout)[0]
        assert not info['HostConfig']['Privileged'] and not info['Mounts']
        assert not info['HostConfig']['PortBindings']
        execute(*DOCKER, 'start', name)
        ssh = [*DOCKER, 'exec', name, 'ssh', '-i', '/work/id_ed25519', '-p', '2222',
               '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
               '-o', 'StrictHostKeyChecking=accept-new',
               '-o', 'ControlMaster=auto', '-o', 'ControlPersist=60',
               '-o', 'ControlPath=/work/ssh-control',
               '-o', 'UserKnownHostsFile=/work/known_hosts', 'ubuntu@127.0.0.1']
        deadline = time.monotonic() + 1500
        while time.monotonic() < deadline:
            try:
                ready = execute(*ssh, 'test -e /run/native-ready', check=False, timeout=15)
                if ready.returncode == 0:
                    break
            except subprocess.TimeoutExpired:
                pass
            state = json.loads(execute(*DOCKER, 'inspect', name).stdout)[0]['State']
            if not state['Running'] or state['OOMKilled']:
                raise RuntimeError('Native VM terminated before readiness')
            print('Waiting for isolated VM bootstrap', flush=True)
            time.sleep(10)
        else:
            raise RuntimeError('Native VM bootstrap deadline exceeded')
        execute(*DOCKER, 'network', 'disconnect', 'bridge', name)
        info = json.loads(execute(*DOCKER, 'inspect', name).stdout)[0]
        assert not info['NetworkSettings']['Networks']
        assert not info['HostConfig']['Privileged'] and not info['Mounts']
        assert not info['HostConfig']['PortBindings']
        result = execute(*ssh, 'sudo python3 /mnt/payload/probe.py', check=False, timeout=300)
        print(result.stdout, end='')
        print(result.stderr, end='')
        if result.returncode == 0 and args.integral:
            for script, timeout in (('fixture.py', 600), ('exercise.py', 1200)):
                print('Native integral stage: ' + script, flush=True)
                result = execute(*ssh, 'sudo python3 -u /mnt/payload/' + script,
                                 check=False, timeout=timeout)
                print(result.stdout, end='')
                print(result.stderr, end='')
                if result.returncode:
                    break
        raise SystemExit(result.returncode)
    finally:
        original = sys.exc_info()[1]
        success = original is None or (isinstance(original, SystemExit) and original.code == 0)
        if cleanup(name, owner) is False and success:
            raise RuntimeError('Native VM cleanup failed')


if __name__ == '__main__':
    main()
