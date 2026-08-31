"""Run the actual HTTP fixture body, including its generated filename/imports."""
import ast
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


class HttpFixtureTests(unittest.TestCase):
    def test_generated_http_fixture_serves_health(self):
        tree = ast.parse(Path(__file__).with_name('fixture.py').read_text())
        candidates = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Name) and node.func.id == 'write'
                      and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                      and isinstance(node.args[1].value, str)
                      and node.args[1].value.startswith('from http.server import')]
        self.assertEqual(len(candidates), 1)
        call = candidates[0]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / Path(ast.literal_eval(call.args[0])).name
            script.write_text(ast.literal_eval(call.args[1]))
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1', 0)); port = reserve.getsockname()[1]
            process = subprocess.Popen([sys.executable, str(script), str(port)], stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail('HTTP fixture exited: ' + process.stderr.read())
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/health', timeout=.2) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), b'{}')
                            return
                    except OSError:
                        time.sleep(.02)
                self.fail('HTTP fixture never became ready')
            finally:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=3)
                process.stderr.close()
