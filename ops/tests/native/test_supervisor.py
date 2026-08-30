"""Prove timeout and termination cleanup using real disposable child processes."""
from pathlib import Path
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import importlib.util
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('supervisor.py')


class SupervisorTests(unittest.TestCase):
    def test_termination_during_spawn_does_not_orphan_child(self):
        spec = importlib.util.spec_from_file_location('vm_supervisor', SCRIPT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        real_popen = subprocess.Popen
        children = []
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        def interrupted_spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs); children.append(child)
            os.kill(os.getpid(), signal.SIGTERM)
            return child
        try:
            with patch.object(sys, 'argv', [str(SCRIPT), '--seconds', '2', sys.executable, '-c', 'import time;time.sleep(10)']), \
                    patch.object(module.subprocess, 'Popen', side_effect=interrupted_spawn):
                self.assertEqual(module.main(), 143)
            self.assertIsNotNone(children[0].poll(), 'Launch interruption orphaned the child')
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill(); child.wait()
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def test_timeout_stops_a_child_that_ignores_alarm(self):
        self.assertTrue(SCRIPT.is_file(), 'External VM supervisor missing')
        result = subprocess.run([sys.executable, str(SCRIPT), '--seconds', '0.2',
                                 sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGALRM,signal.SIG_IGN);time.sleep(10)'],
                                timeout=3, capture_output=True)
        self.assertEqual(result.returncode, 124)

    def test_sigterm_reaps_child(self):
        self.assertTrue(SCRIPT.is_file(), 'External VM supervisor missing')
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'pid'
            child = f'import os,time;from pathlib import Path;Path({str(pid_file)!r}).write_text(str(os.getpid()));time.sleep(10)'
            process = subprocess.Popen([sys.executable, str(SCRIPT), '--seconds', '5', sys.executable, '-c', child])
            try:
                deadline = time.monotonic() + 2
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(pid_file.exists())
                child_pid = int(pid_file.read_text())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=3), 143)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=3)
