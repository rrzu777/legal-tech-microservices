"""Continuous shell leases and non-cancelling drain against real protocol files."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import re
import shutil

import pytest

from test_worker_maintenance_cli import host, ROOT, BOOT, INSTANCE
from worker.maintenance_store import Ack

LIB = ROOT / "ops/worker-maintenance.sh"
NEW_BOUNDARIES = ("app/ojv/__init__.py", "app/ojv/session.py", "app/ojv/browser_login.py",
                  "app/playwright_runtime.py", "worker/maintenance_heartbeat.py", "worker/proxy_control.py")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="production GNU flock and fdinfo")


def shell(host, code, *, timeout=15, extra=None):
    assert LIB.is_file(), "common shell admission missing"
    root, _, args = host
    env = dict(os.environ, WM_TEST_MODE="1", WM_PYTHON=sys.executable, WM_FLOCK="/usr/bin/flock",
               WM_DATE="/bin/date", WM_SLEEP="/bin/sleep", WM_POLL_ATTEMPTS="3", WM_POLL_SECONDS="0",
               WM_CONTROL_DIR=str(root / "control"), WM_ACK_DIR=str(root / "ack"),
               WM_PROC_ROOT=str(root / "proc"), WM_SYSTEMCTL=str(root / "systemctl"),
               WM_GLOBAL_LOCK=str(root / "global.lock"), WM_JOURNAL_ROOT=str(root / "journals"),
               WM_HEALTH_URL=(root / "health").as_uri(), WM_ROOT_UID=str(os.getuid()), WM_ROOT_GID=str(os.getgid()),
               WM_WORKER_UID=str(os.getuid()), WM_WORKER_GID=str(os.getgid()))
    # Strict fake clock, not a production window override.
    date = root / "date"
    date.write_text("#!/bin/sh\nprintf '20\\n'\n")
    date.chmod(0o755)
    env["WM_DATE"] = str(date)
    if extra:
        env.update(extra)
    return subprocess.run(["bash", "-c", f'set -euo pipefail; source "{LIB}"; wm_init; trap wm_close EXIT; {code}'],
                          env=env, capture_output=True, text=True, timeout=timeout)


def test_missing_capability_before_any_mutation(host):
    (host[0] / "ack/ack.json").unlink()
    result = shell(host, f'wm_acquire_global; wm_prepare; touch "{host[0]}/mutated"')
    assert result.returncode != 0
    assert not (host[0] / "mutated").exists()
    assert host[1].read_control().state == "open"


def test_timeout_keeps_hold_never_mutates_or_stops(host):
    result = shell(host, f'wm_acquire_global; wm_prepare; touch "{host[0]}/mutated"')
    assert result.returncode != 0
    assert host[1].read_control().state == "hold"
    assert not (host[0] / "mutated").exists()


def test_prepared_admission_is_continuous_through_child_and_finish(host):
    ended = threading.Event()
    def acknowledge():
        while not ended.wait(0.005):
            control = host[1].read_control()
            if control.state == "hold":
                host[1].write_ack(Ack(1, control.operation_id, BOOT, 512, 9012, INSTANCE, "quiescent", 0))
    thread = threading.Thread(target=acknowledge)
    thread.start()
    try:
        result = shell(host, f'''wm_acquire_global; wm_prepare
            ! flock -n "{host[0]}/global.lock" true
            ! flock -n "{host[0]}/control/admission.lock" true
            wm_delegate bash -c 'source "{LIB}"; wm_init; trap wm_close EXIT; wm_acquire_global; wm_prepare'
            ! flock -n "{host[0]}/global.lock" true
            ! flock -n "{host[0]}/control/admission.lock" true
            wm_finish''', extra={"WM_POLL_ATTEMPTS": "20"})
    finally:
        ended.set()
        thread.join()
    assert result.returncode == 0, result.stderr
    assert host[1].read_control().state == "open"


def test_global_lock_rejects_parallel_mutator_for_whole_transaction(host):
    import fcntl
    with open(host[0] / "global.lock", "rb") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = shell(host, f'wm_acquire_global; touch "{host[0]}/mutated"')
    assert result.returncode != 0
    assert not (host[0] / "mutated").exists()


@pytest.mark.parametrize("invalid", ["fifo", "mode", "owner"])
def test_global_lock_rejects_unsafe_file_before_shell_open(host, invalid):
    lock = host[0] / "global.lock"
    if invalid == "fifo":
        lock.unlink()
        os.mkfifo(lock, 0o600)
    elif invalid == "mode":
        lock.chmod(0o666)
    else:
        if os.getuid() != 0:
            pytest.skip("root ownership fixture")
        os.chown(lock, 12345, 12345)
    result = shell(host, f'wm_acquire_global; touch "{host[0]}/mutated"', timeout=2)
    assert result.returncode != 0
    assert not (host[0] / "mutated").exists()


def test_slow_verification_exhausts_monotonic_deadline_not_900_retries(host):
    clock = host[0] / "clock-python"
    clock.write_text(f'''#!{sys.executable}
import os, pathlib, sys
root = pathlib.Path({str(host[0])!r})
if len(sys.argv)>2 and sys.argv[1]=='-c' and 'time.monotonic' in sys.argv[2]:
    count = int((root/'ticks').read_text()) if (root/'ticks').exists() else 0
    (root/'ticks').write_text(str(count+1))
    print(0 if count<2 else 900)
elif 'verify-ack' in sys.argv:
    with (root/'verify-calls').open('a') as out: out.write('verify\\n')
    os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
else:
    os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
    clock.chmod(0o755)
    result = shell(host, 'wm_acquire_global; wm_prepare', extra={"WM_PYTHON": str(clock)})
    assert result.returncode != 0
    assert host[1].read_control().state == "hold"
    assert (host[0] / "verify-calls").read_text().splitlines() == ["verify"]


def test_rollback_contract_checks_bytes_not_module_presence(host):
    repo = host[0] / "repo"
    worker = repo / "estrado-pjud-service/worker"
    worker.mkdir(parents=True)
    for filename in ("__main__.py", "maintenance.py", "maintenance_store.py", "metrics.py", "sd_notify.py"):
        (worker / filename).write_text("original compatible bytes\n")
    for relative in ("worker/__init__.py", "worker/config.py", "worker/session_pool.py", "app/__init__.py", "app/r2.py", "app/minter.py", *NEW_BOUNDARIES):
        target = repo / "estrado-pjud-service" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original compatible bytes\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "baseline"], check=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    result = shell(host, f'wm_pin_runtime; wm_pin_worker_contract "{repo}"; wm_check_worker_contract "{repo}" "{revision}"')
    assert result.returncode == 0, result.stderr
    (worker / "__main__.py").write_text("legacy worker without admission\n")
    result = shell(host, f'wm_pin_runtime; wm_pin_worker_contract "{repo}"; wm_check_worker_contract "{repo}" "{revision}"')
    assert result.returncode != 0


@pytest.mark.parametrize("relative", NEW_BOUNDARIES)
@pytest.mark.parametrize("change", ["edit", "remove"])
@pytest.mark.parametrize("boundary", ["incoming", "rollback"])
def test_each_runtime_dependency_rejected_before_merge_or_restore(host, relative, change, boundary):
    repo = host[0] / "contract-repo"
    service = repo / "estrado-pjud-service"
    for path in ("worker/__init__.py", "worker/__main__.py", "worker/maintenance.py",
                 "worker/maintenance_store.py", "worker/metrics.py", "worker/sd_notify.py",
                 "worker/config.py", "worker/session_pool.py", "app/__init__.py", "app/r2.py",
                 "app/minter.py", *NEW_BOUNDARIES):
        target = service / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "estrado-pjud-service" / path, target)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    def commit():
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "fixture"], check=True)
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    commit()
    installed = host[0] / "installed"
    shutil.copytree(service, installed / "estrado-pjud-service")
    changed = service / relative
    if change == "edit":
        changed.write_text(changed.read_text() + "\n# incompatible ownership\n")
    else:
        changed.unlink()
    incompatible = commit()
    if boundary == "rollback":
        # Invoke the actual deploy rollback function. Its byte check must fail
        # before reset or dependency work; the incompatible ref is the target.
        source = (ROOT / "ops/deploy.sh").read_text()
        function = re.search(r"^rollback_code_and_dependencies\(\) \{.*?^\}", source, re.S | re.M).group(0)
        code = function + f'\nrepo_dir="{repo}"; cd "$repo_dir"; prev="{incompatible}"; deps_changed=0; rollback_code_and_dependencies'
        # Make rollback observable: current HEAD differs from its bad target.
        (service / "api-only.txt").write_text("safe unrelated API change")
        current = commit()
    else:
        # The target ref is compared against the captured installed bytes.
        current = incompatible
        code = f'wm_check_worker_contract "{repo}" "{incompatible}"'
    marker = host[0] / "lifecycle-started"
    result = shell(host, f'wm_pin_worker_contract "{installed}"; {code}; touch "{marker}"')
    assert "maintenance" in result.stderr, result.stderr
    assert result.returncode != 0, (relative, change, boundary)
    assert not marker.exists()
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == current


@pytest.mark.parametrize("script", ["deploy.sh", "provision.sh"])
def test_standalone_mutator_rejects_missing_capability_before_shared_state_or_units(host, script):
    (host[0] / "ack/ack.json").unlink()
    shared = host[0] / "state"
    shared.mkdir()
    alert = shared / "alert-cooldowns.json"
    alert.touch(mode=0o640)
    repo = host[0] / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # A controlled git checkout plus fake unit client. The canonical helper is
    # sourced by the actual script, never by a fake replacement mutator.
    result = shell(host, f'bash "{ROOT}/ops/{script}"', extra={
        "DEPLOY_REPO_DIR": str(repo), "DEPLOY_SYSTEMCTL": str(host[0] / "systemctl"),
        "DEPLOY_STATE_DIR": str(shared), "PROV_REPO_DIR": str(repo),
        "PROV_SYSTEMCTL": str(host[0] / "systemctl")})
    assert result.returncode != 0
    assert "maintenance" in result.stderr
    assert alert.stat().st_mode & 0o777 == 0o640
    assert host[1].read_control().state == "open"
