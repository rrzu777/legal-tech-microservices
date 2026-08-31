"""Opt-in Linux regression for detached descendants at the real deploy boundary.

Run only in an isolated Linux test environment with PID/network namespace
permission: PJUD_RUN_PROCESS_TESTS=1 python3 this-file.py. No browsers/network.
Unlike shell doubles this executes real GNU timeout, unshare and a setsid child.
"""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("PJUD_RUN_PROCESS_TESTS") == "1",
                     "requires opt-in isolated Linux PID/network namespace environment")
class RuntimeProcessTests(unittest.TestCase):
    def test_timeout_leaves_no_detached_stubborn_descendant(self):
        # Adopt/reap our own orphaned fixtures, including on the expected RED.
        self.assertEqual(ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0), 0)
        with tempfile.TemporaryDirectory(prefix="runtime-process-") as folder:
            root = Path(folder)
            service = root / "service"
            (service / ".venv/bin").mkdir(parents=True)
            (service / ".venv/bin/python").symlink_to(sys.executable)
            cache = root / "cache"
            cache.mkdir()
            marker = root / "descendant-pid"
            init_marker = root / "init-pids"
            # These replace only identity/display; namespace and timeout are
            # real. The timeout adapter checks production bounds then shortens
            # wall time so RED/GREEN don't each cost 65 seconds.
            scripts = {
                "runuser": '#!/bin/bash\nshift 5\nexec "$@"\n',
                "xvfb": '#!/bin/bash\n[ "$1" = -a ] || exit 64\nshift\nexec "$@"\n',
                "timeout": ('#!/bin/bash\n[ "$1" = --kill-after=5s ] && [ "$2" = 60s ] || exit 64\n'
                            'shift 2\nexec /usr/bin/timeout --kill-after=0.5s 2s "$@"\n'),
            }
            for name, content in scripts.items():
                path = root / name
                path.write_text(content)
                path.chmod(0o755)
            child = (
                "import os, pathlib, signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                # /proc belongs to the container's outer PID namespace: first
                # NSpid is the PID visible to this test, even inside nested PID.
                "status=pathlib.Path('/proc/self/status').read_text(); "
                "pid=next(line.split()[1] for line in status.splitlines() if line.startswith('NSpid:')); "
                f"pathlib.Path({str(marker)!r}).write_text(pid); "
                "time.sleep(120)"
            )
            smoke = (
                "import pathlib, signal, subprocess, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "status=pathlib.Path('/proc/self/status').read_text().splitlines(); "
                "pids=[next(line.split()[1] for line in status if line.startswith(key)) for key in ('NSpid:', 'PPid:')]; "
                f"pathlib.Path({str(init_marker)!r}).write_text(' '.join(pids)); "
                f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True, "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "time.sleep(120)"
            )
            deploy = Path(__file__).resolve().parents[1] / "deploy.sh"
            source = deploy.read_text()
            # Load the real functions, without executing deployment/main. This
            # fixture never has git/systemctl access or any real service path.
            self.assertTrue(source.rstrip().endswith('main "$@"'))
            source = source.rsplit('main "$@"', 1)[0]
            harness = source + '\nverify_playwright_runtime\n'
            env = dict(os.environ, service_dir=str(service),
                       playwright_browsers_path=str(cache), allow_test_browser_path="1",
                       browser_owner_uid=str(os.getuid()), browser_owner_gid=str(os.getgid()),
                       find_bin="/usr/bin/find", timeout_bin=str(root / "timeout"),
                       unshare_bin="/usr/bin/unshare", runuser_bin=str(root / "runuser"),
                       xvfb_run_bin=str(root / "xvfb"), runtime_smoke_code=smoke)
            owned_pid = None
            pid_observed = False
            wrapper_pids = []
            try:
                result = subprocess.run(["/bin/bash", "-c", harness], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertTrue(marker.is_file(), "fixture never launched: namespace/adapter unavailable")
                owned_pid = int(marker.read_text())
                pid_observed = True
                wrapper_pids = [int(pid) for pid in init_marker.read_text().split()]
                self.assertEqual(result.returncode, 1)
                self.assertIn("browser_unavailable", result.stderr)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        waited, _ = os.waitpid(owned_pid, os.WNOHANG)
                    except ChildProcessError:
                        waited = 0
                    if waited == owned_pid or not Path(f"/proc/{owned_pid}").exists():
                        owned_pid = None
                        break
                    time.sleep(0.02)
                self.assertIsNone(owned_pid, "detached descendant survived runtime timeout")
                for pid in wrapper_pids:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
                    self.assertFalse(Path(f"/proc/{pid}").exists(), "namespace init/unshare survived timeout")
            finally:
                if not pid_observed and marker.is_file():
                    # If an earlier assertion failed, recover only this owned
                    # fixture. Never scan/kill browser processes globally.
                    owned_pid = int(marker.read_text())
                if owned_pid is not None:
                    try:
                        os.kill(owned_pid, signal.SIGKILL)
                        os.waitpid(owned_pid, 0)
                    except (ProcessLookupError, ChildProcessError):
                        pass
                for pid in wrapper_pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                    except (ProcessLookupError, ChildProcessError):
                        pass


if __name__ == "__main__":
    unittest.main()
