"""Execute the real heartbeat fence with real jq; transport and clock are fixtures."""
from pathlib import Path
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'resource-guards.sh'


def heartbeat(proxy_mode, metadata, require_zero=0):
    text = SOURCE.read_text()
    functions = []
    for name in ('is_uint', 'worker_heartbeat_is_idle'):
        match = re.search(r'^' + name + r'\(\) \{.*?^\}', text, re.M | re.S)
        if not match:
            raise AssertionError('Missing heartbeat helper ' + name)
        functions.append(match.group())
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, 'heartbeat.body').write_text(json.dumps([{
            'status': 'idle_off_hours', 'last_heartbeat_at': '2026-08-31T01:00:00Z', 'metadata': metadata}]))
        script = '''
write_curl_config() { return 0; }
fixture_curl() { printf 200; }
fixture_date() { case "${@: -1}" in +%s) echo 1788138000 ;; +%s%N) echo 1788138000000000000 ;; *) return 2 ;; esac; }
curl_bin=fixture_curl
date_bin=fixture_date
null_file=/dev/null
SUPABASE_URL=http://fixture.invalid
worker_id_encoded=fixture
HEARTBEAT_MAX_AGE_SECONDS=300
'''
        script += '\n'.join(functions)
        script += '\ntemp_dir=' + shlex.quote(directory)
        script += '\njq_bin=' + shlex.quote(shutil.which('jq') or '/missing/jq')
        script += f'\nworker_proxy_mode={proxy_mode}\nworker_heartbeat_is_idle {require_zero}\n'
        return subprocess.run(['bash', '-c', script], env={'PATH': os.environ['PATH']}, capture_output=True, text=True)


class HeartbeatJqTests(unittest.TestCase):
    def test_idle_direct_worker_does_not_require_proxy_metadata(self):
        result = heartbeat(0, {'process_outside_office_hours_enabled': False, 'mint_attempts': 0})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_proxy_worker_requires_enabled_status_and_null_reason(self):
        base = {'process_outside_office_hours_enabled': False, 'mint_attempts': 0}
        self.assertEqual(heartbeat(1, base | {'proxy_control_status': 'enabled', 'proxy_control_reason': None}).returncode, 0)
        for extra in ({}, {'proxy_control_status': 'disabled'},
                      {'proxy_control_status': 'enabled', 'proxy_control_reason': 'blocked'}):
            with self.subTest(extra=extra):
                self.assertNotEqual(heartbeat(1, base | extra).returncode, 0)

    def test_direct_worker_still_rejects_paid_or_out_of_hours_activity(self):
        for metadata in ({'process_outside_office_hours_enabled': True, 'mint_attempts': 0},
                         {'process_outside_office_hours_enabled': False, 'mint_attempts': 1}):
            with self.subTest(metadata=metadata):
                self.assertNotEqual(heartbeat(0, metadata, require_zero=1).returncode, 0)
