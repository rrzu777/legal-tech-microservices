"""Harmless application doubles for an integral rollout on a disposable guest.

Only application bodies and HTTP endpoints are doubles. systemd, cgroups,
filesystem metadata, git, provisioning, locking and swap are the real tools.
Never run on the VPS: guest identity and virtualization are mandatory gates.
"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess

REPO = Path('/opt/legal-tech-microservices')


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def write(path, body, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)


def fixture_values(inventory):
    values = {name: 'native-fixture-only' for name in inventory.splitlines()
              if name and not name.startswith('#')}
    values.update(SUPABASE_URL='http://127.0.0.1:9080', WORKER_ID='native-worker',
                  COOKIE_STORE_PATH='/var/lib/estrado-pjud/cookies.json',
                  OJV_PROXY_URL='', PJUD_PROCESS_OUTSIDE_OFFICE_HOURS='false',
                  PJUD_OFF_HOURS_VALIDATION_ONCE='false')
    return values


def main():
    if os.geteuid() != 0 or Path('/etc/hostname').read_text().strip() != 'native-guards':
        raise SystemExit('Not the isolated validation guest')
    if run('systemd-detect-virt') != 'qemu' or REPO.exists():
        raise SystemExit('Refusing non-QEMU or reused fixture')
    if Path('/proc/swaps').read_text().count('\n') != 1:
        raise SystemExit('Guest already has swap')
    run('useradd', '--system', '--create-home', 'estrado')
    run('useradd', '--uid', '1002', '--create-home', 'hermes')
    run('usermod', '-aG', 'estrado', 'www-data')
    REPO.mkdir()
    shutil.copytree('/mnt/payload/ops', REPO / 'ops')
    write(REPO / '.gitignore', 'estrado-pjud-service/\n')
    application = REPO / 'estrado-pjud-service'
    application.mkdir()
    values = fixture_values((REPO / 'ops/env.inventory').read_text())
    write(application / '.env', ''.join(f'{key}={value}\n' for key, value in values.items()), 0o640)
    run('chown', 'root:estrado', str(application / '.env'))
    (application / 'logs').mkdir()
    run('chown', 'estrado:estrado', str(application / 'logs'))
    # No application code/import, browser, RPC, proxy or real credential exists.
    write(application / '.venv/bin/python', '#!/bin/sh\nexec /usr/bin/python3 /opt/native-fixture/worker.py\n', 0o755)
    write(application / '.venv/bin/uvicorn', '#!/bin/sh\nexec /usr/bin/python3 /opt/native-fixture/fixture_http_server.py 8000\n', 0o755)
    write('/opt/estrado-cron/run-cron.sh', '#!/bin/sh\nexit 0\n', 0o755)
    write('/opt/native-fixture/worker.py', '''import datetime, json, os, socket, time
from pathlib import Path
address = os.environ.get('NOTIFY_SOCKET')
if address:
    address = '\\0' + address[1:] if address.startswith('@') else address
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    client.connect(address)
    client.send(b'READY=1')
while True:
    heartbeat = [{'status':'idle_off_hours',
      'last_heartbeat_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
      'metadata':{'process_outside_office_hours_enabled':False,'mint_attempts':0}}]
    destination = Path('/opt/legal-tech-microservices/estrado-pjud-service/logs/heartbeat.json')
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(heartbeat))
    temporary.replace(destination)
    if address: client.send(b'WATCHDOG=1')
    time.sleep(1)
''')
    write('/opt/native-fixture/fixture_http_server.py', '''from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/rest/v1/sync_worker_heartbeats?'):
            body = Path('/opt/legal-tech-microservices/estrado-pjud-service/logs/heartbeat.json').read_bytes()
        else: body = b'{}'
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def do_HEAD(self):
        self.send_response(200); self.send_header('Content-Range','*/0'); self.end_headers()
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
''')
    write('/etc/systemd/system/native-http.service', '[Service]\nExecStart=/usr/bin/python3 /opt/native-fixture/fixture_http_server.py 9080\n')
    write('/etc/systemd/system/legaltech.slice', '[Slice]\nCPUWeight=1000\n')
    for unit in ('estrado-pjud.service', 'estrado-pjud-worker.service'):
        body = (REPO / 'ops/systemd' / unit).read_text()
        # Representative legacy layout: worker outside the protected slice.
        if unit.endswith('-worker.service'):
            body = body.replace('PartOf=legaltech.slice\n', '').replace('Slice=legaltech.slice', 'Slice=system.slice')
        else:
            body = body.replace('CPUWeight=500', 'CPUWeight=100')
        write('/etc/systemd/system/' + unit, body)
    for unit in ('legaltech-monitor.service', 'legaltech-resource-tracker.service'):
        write('/etc/systemd/system/' + unit,
              '[Unit]\nPartOf=legaltech.slice\n[Service]\nType=simple\n'
              'Slice=legaltech.slice\nExecStart=/usr/bin/sleep infinity\n'
              '[Install]\nWantedBy=multi-user.target\n')
    for unit in ('hermes-gateway.service', 'hermes-dashboard.service'):
        write('/home/hermes/.config/systemd/user/' + unit,
              '[Service]\nExecStart=/usr/bin/sleep infinity\n[Install]\nWantedBy=default.target\n')
    run('chown', '-R', 'hermes:hermes', '/home/hermes/.config')
    run('loginctl', 'enable-linger', 'hermes')
    run('systemctl', 'start', 'user@1002.service')
    run('systemctl', '--user', '--machine=hermes@.host', 'daemon-reload')
    run('systemctl', '--user', '--machine=hermes@.host', 'enable', '--now',
        'hermes-gateway.service', 'hermes-dashboard.service')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'start', 'native-http.service')
    run('systemctl', 'enable', '--now', 'estrado-pjud.service', 'estrado-pjud-worker.service',
        'legaltech-monitor.service', 'legaltech-resource-tracker.service')
    run('git', '-C', str(REPO), 'init', '-q')
    run('git', '-C', str(REPO), 'add', '.gitignore', 'ops')
    run('git', '-C', str(REPO), '-c', 'user.name=Native Fixture',
        '-c', 'user.email=native@example.invalid', 'commit', '-qm', 'native fixture payload')
    # Resolve the complete explicit test boundary to the real guest defaults.
    # Only health URLs differ; the local HTTP server represents external APIs.
    text = (REPO / 'ops/resource-guards.sh').read_text()
    variables, env = {}, {'RG_TEST_MODE': '1'}
    for variable, key, value in re.findall(r'^(\w+)=\$\{(RG_\w+):-([^}]*)\}$', text, re.M):
        for previous, replacement in variables.items():
            value = value.replace('$' + previous, replacement)
        if '$' in value:
            raise RuntimeError('Unresolved native test default')
        variables[variable], env[key] = value, value
    names = re.search(r'readonly -a OVERRIDE_NAMES=\((.*?)\)', text, re.S).group(1).split()
    if set(names) - env.keys():
        raise RuntimeError('Incomplete native test boundary')
    env.update(RG_TEST_MODE='1', RG_JURISTRACK_HEALTH_URL='http://127.0.0.1:9080/',
               RG_ESTRADO_HEALTH_URL='http://127.0.0.1:8000/api/v1/health')
    write('/opt/native-fixture/environment.json', json.dumps(env), 0o600)
    print('NATIVE FIXTURE READY sha=' + run('git', '-C', str(REPO), 'rev-parse', 'HEAD'))


if __name__ == '__main__':
    main()
