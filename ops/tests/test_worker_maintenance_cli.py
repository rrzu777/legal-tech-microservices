"""Operator protocol integration: real files/locks, fake systemd and kernel input."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "ops/worker-maintenance.py"
sys.path.insert(0, str(ROOT / "estrado-pjud-service"))
from worker.maintenance_store import Ack, Control, MaintenanceStore, StorePolicy

OLD = "64a8eb10-2d55-457f-924c-23d5a532c847"
OP = "71ae117a-610b-46da-9766-3841100f8710"
BOOT = "f784c8bd-67c3-448e-ae1c-55ac6feab947"
INSTANCE = "bf763d76-b99c-464d-80d8-bcbd9520b923"
IDENTITY = f"{BOOT}:512:9012:{INSTANCE}"


@pytest.fixture
def host(tmp_path):
    root = tmp_path.resolve()
    control, ack, proc = (root / part for part in ("control", "ack", "proc"))
    control.mkdir(mode=0o750)
    ack.mkdir(mode=0o700)
    (control / "admission.lock").touch(mode=0o640)
    for path in (control, ack, control / "admission.lock"):
        os.chown(path, os.getuid(), os.getgid())
    store = MaintenanceStore(control, ack, StorePolicy(os.getuid(), os.getgid(), os.getuid(), os.getgid()), allow_control_writes=True)
    store.initialize_hold(OLD)
    store.transition(OLD, "hold", Control(1, "open", OLD, "2026-08-30T00:00:00Z"))
    store.write_ack(Ack(1, OLD, BOOT, 512, 9012, INSTANCE, "draining", 0))
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text(BOOT)
    (proc / "512").mkdir()
    (proc / "512/stat").write_text("512 (worker) " + " ".join(["S"] + ["0"] * 18 + ["9012"]))
    (proc / "512/cgroup").write_text("0::/legaltech.slice/estrado-pjud-worker.service\n")
    (root / "systemd").write_text("ActiveState=active\nMainPID=512\nControlGroup=/legaltech.slice/estrado-pjud-worker.service\nSlice=legaltech.slice\nResult=success\n")
    systemctl = root / "systemctl"
    systemctl.write_text(f"#!/bin/sh\ncase \"$1\" in show) cat '{root}/systemd';; is-active) echo active;; *) exit 2;; esac\n")
    systemctl.chmod(0o755)
    global_lock = root / "global.lock"
    global_lock.touch(mode=0o600)
    health = root / "health"
    health.write_text("ok")
    args = ["--test-mode", "--control-dir", str(control), "--ack-dir", str(ack),
            "--proc-root", str(proc), "--systemctl", str(systemctl), "--global-lock", str(global_lock),
            "--journal-root", str(root / "journals"), "--health-url", health.as_uri(),
            "--root-uid", str(os.getuid()), "--root-gid", str(os.getgid()),
            "--worker-uid", str(os.getuid()), "--worker-gid", str(os.getgid())]
    return root, store, args


def run(host, *args, fds=()):
    assert CLI.is_file(), "operator CLI must exist before protocol mutations"
    return subprocess.run([sys.executable, str(CLI), *host[2], *args],
                          pass_fds=fds, capture_output=True, text=True)


def hold(host):
    result = run(host, "begin", "--operation-id", OP, "--identity", IDENTITY)
    assert result.returncode == 0, result.stderr
    host[1].write_ack(Ack(1, OP, BOOT, 512, 9012, INSTANCE, "quiescent", 0))


def test_status_authenticates_capability_not_quiescence(host):
    result = run(host, "status", "--require-open")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"open {OLD} {IDENTITY}"


def test_missing_legacy_ack_rejected_before_journal_or_hold(host):
    (host[0] / "ack/ack.json").unlink()
    result = run(host, "begin", "--operation-id", OP, "--identity", IDENTITY)
    assert result.returncode == 1
    assert host[1].read_control().state == "open"
    assert not (host[0] / "journals").exists()


def test_begin_persists_exact_intent_then_hold_and_refuses_foreign_owner(host):
    hold(host)
    journal = json.loads((host[0] / f"journals/{OP}.json").read_text())
    assert journal["operation_id"] == OP
    assert journal["initial_identity"] == IDENTITY
    assert journal["result"] == "intended"
    result = run(host, "begin", "--operation-id", str(uuid.uuid4()), "--identity", IDENTITY)
    assert result.returncode == 1
    assert host[1].read_control().operation_id == OP


@pytest.mark.parametrize("failure", ["draining", "nonce", "pid", "boot", "ticks", "cgroup", "busy"])
def test_verify_rejects_unsafe_proof_and_preserves_hold(host, failure):
    hold(host)
    if failure == "draining":
        host[1].write_ack(Ack(1, OP, BOOT, 512, 9012, INSTANCE, "draining", 0))
    elif failure == "nonce":
        host[1].write_ack(Ack(1, OP, BOOT, 512, 9012, str(uuid.uuid4()), "quiescent", 0))
    elif failure == "pid":
        path = host[0] / "systemd"
        path.write_text(path.read_text().replace("MainPID=512", "MainPID=513"))
    elif failure == "boot":
        (host[0] / "proc/sys/kernel/random/boot_id").write_text(str(uuid.uuid4()))
    elif failure == "ticks":
        path = host[0] / "proc/512/stat"
        path.write_text(path.read_text().replace("9012", "9013"))
    elif failure == "cgroup":
        (host[0] / "proc/512/cgroup").write_text("0::/foreign.service\n")
    if failure == "busy":
        with host[1].shared_lease():
            result = run(host, "verify-ack", "--operation-id", OP, "--identity", IDENTITY)
    else:
        result = run(host, "verify-ack", "--operation-id", OP, "--identity", IDENTITY)
    assert result.returncode == 1
    assert host[1].read_control().state == "hold"
    assert "Traceback" not in result.stderr


def test_finish_requires_health_exact_uuid_and_is_idempotent(host):
    hold(host)
    wrong = run(host, "finish", "--operation-id", OLD, "--identity", IDENTITY)
    assert wrong.returncode == 1
    (host[0] / "health").unlink()
    failed = run(host, "finish", "--operation-id", OP, "--identity", IDENTITY)
    assert failed.returncode == 1
    assert host[1].read_control().state == "hold"
    (host[0] / "health").write_text("ok")
    assert run(host, "finish", "--operation-id", OP, "--identity", IDENTITY).returncode == 0
    assert host[1].read_control().state == "open"
    assert json.loads((host[0] / f"journals/{OP}.json").read_text())["result"] == "succeeded"
    assert run(host, "finish", "--operation-id", OP, "--identity", IDENTITY).returncode == 0


def test_partial_overrides_and_unheld_inherited_fd_rejected(host):
    assert CLI.is_file(), "operator CLI missing"
    partial = subprocess.run([sys.executable, str(CLI), "--test-mode", "--control-dir", str(host[0]), "status"], capture_output=True)
    assert partial.returncode == 2
    with open(host[0] / "global.lock", "rb") as unheld:
        result = run(host, "begin", "--operation-id", OP, "--identity", IDENTITY,
                     "--global-fd", str(unheld.fileno()), fds=(unheld.fileno(),))
    assert result.returncode == 1
    assert host[1].read_control().state == "open"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux fdinfo proves exact inherited flock ownership")
def test_inherited_global_ex_is_not_unlocked_by_child(host):
    with open(host[0] / "global.lock", "rb") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(host, "begin", "--operation-id", OP, "--identity", IDENTITY,
                     "--global-fd", str(lease.fileno()), fds=(lease.fileno(),))
        assert result.returncode == 0, result.stderr
        with open(host[0] / "global.lock", "rb") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_post_rename_release_error_is_uncertain_after_durable_success(host, monkeypatch, capsys):
    hold(host)
    spec = importlib.util.spec_from_file_location("operator_cli", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_sync = os.fsync
    calls = []
    def fail_open_directory_sync(fd):
        if os.fstat(fd).st_ino == (host[0] / "control").stat().st_ino and host[1].read_control().state == "open":
            journal = json.loads((host[0] / f"journals/{OP}.json").read_text())
            calls.append(journal["result"])
            raise OSError("simulated directory persistence failure")
        return original_sync(fd)
    monkeypatch.setattr(os, "fsync", fail_open_directory_sync)
    result = module.main([*host[2], "finish", "--operation-id", OP, "--identity", IDENTITY])
    assert result == 3
    assert calls == ["succeeded"]
    assert host[1].read_control().state == "open"
    assert "may already be open" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_output_failure_after_open_is_postpublication_without_control_rewrite(host, monkeypatch, capsys, failure):
    hold(host)
    spec = importlib.util.spec_from_file_location("operator_cli_output", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = MaintenanceStore.transition
    transitions = []
    def transition(store, *args):
        transitions.append(args[2].state)
        return original(store, *args)
    class BrokenOutput:
        def write(self, value):
            if failure == "write":
                raise OSError("private-output-sentinel")
            return len(value)
        def flush(self):
            raise OSError("private-output-sentinel")
    with monkeypatch.context() as scoped:
        scoped.setattr(MaintenanceStore, "transition", transition)
        scoped.setattr(sys, "stdout", BrokenOutput())
        result = module.main([*host[2], "finish", "--operation-id", OP, "--identity", IDENTITY])
    assert result == 3
    assert transitions == ["open"]
    assert host[1].read_control().state == "open"
    assert json.loads((host[0] / f"journals/{OP}.json").read_text())["result"] == "succeeded"
    diagnostic = capsys.readouterr().err
    assert "may already be open" in diagnostic
    assert "hold is not released" not in diagnostic
    assert "private-output-sentinel" not in diagnostic


def test_real_closed_stdout_pipe_keeps_postpublication_exit_three_not_atexit_120(host):
    hold(host)
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run([sys.executable, str(CLI), *host[2], "finish",
                                 "--operation-id", OP, "--identity", IDENTITY],
                                stdout=writer, stderr=subprocess.PIPE, text=True)
    finally:
        os.close(writer)
    assert result.returncode == 3
    assert host[1].read_control().state == "open"
    assert "may already be open" in result.stderr
    assert "hold is not released" not in result.stderr
    assert "Exception ignored" not in result.stderr


def test_both_real_closed_output_pipes_cannot_override_postpublication_exit_three(host):
    hold(host)
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run([sys.executable, str(CLI), *host[2], "finish",
                                 "--operation-id", OP, "--identity", IDENTITY],
                                stdout=writer, stderr=writer)
    finally:
        os.close(writer)
    assert result.returncode == 3
    assert host[1].read_control().state == "open"
    assert json.loads((host[0] / f"journals/{OP}.json").read_text())["result"] == "succeeded"


@pytest.mark.parametrize("payload", [
    '{"version":1,"operation_id":"' + OP + '","initial_identity":"' + IDENTITY + '","result":"intended","result":"succeeded"}',
    '{"version":true,"operation_id":"' + OP + '","initial_identity":"' + IDENTITY + '","result":"succeeded"}',
])
def test_recovery_rejects_ambiguous_journal(host, payload):
    hold(host)
    (host[0] / f"journals/{OP}.json").write_text(payload)
    result = run(host, "finish", "--operation-id", OP, "--identity", IDENTITY)
    assert result.returncode == 1
    assert host[1].read_control().state == "hold"


def test_journal_identity_type_error_is_sanitized(host):
    hold(host)
    path = host[0] / f"journals/{OP}.json"
    value = json.loads(path.read_text())
    value["initial_identity"] = {"secret": "must-not-print"}
    path.write_text(json.dumps(value))
    result = run(host, "finish", "--operation-id", OP, "--identity", IDENTITY)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "must-not-print" not in result.stderr


@pytest.mark.parametrize("change", ["legacy", "wrong-user", "wrong-runtime", "unsafe-prestart", "incompatible-exec"])
def test_restore_unit_rejects_incompatible_worker_before_mutation(host, change):
    value = (ROOT / "ops/systemd/estrado-pjud-worker.service").read_text()
    if change == "legacy":
        value = value.replace("RuntimeDirectory=worker-maintenance\n", "")
    elif change == "wrong-user":
        value = value.replace("User=estrado", "User=root")
    elif change == "wrong-runtime":
        value = value.replace("RuntimeDirectoryMode=0700", "RuntimeDirectoryMode=0755")
    elif change == "unsafe-prestart":
        value = value.replace("[Service]", "[Service]\nExecStartPre=/opt/legacy-worker")
    elif change == "incompatible-exec":
        value = value.replace("python -m worker", "python -m legacy_worker")
    unit = host[0] / "worker.service"
    unit.write_text(value)
    unit.chmod(0o644)
    result = run(host, "status", "--unit-file", str(unit))
    assert result.returncode == 1


def test_compatible_unit_and_xvfb_dropin_preserve_protocol(host):
    unit, dropin = host[0] / "worker.service", host[0] / "xvfb.conf"
    unit.write_text((ROOT / "ops/systemd/estrado-pjud-worker.service").read_text())
    dropin.write_text((ROOT / "ops/systemd/estrado-pjud-worker.service.d/xvfb.conf").read_text())
    unit.chmod(0o644)
    dropin.chmod(0o644)
    result = run(host, "status", "--unit-file", str(unit), "--dropin-file", str(dropin))
    assert result.returncode == 0, result.stderr


def test_global_lock_supports_root_sticky_run_lock_directory(host):
    directory = host[0] / "run-lock"
    directory.mkdir()
    directory.chmod(0o1777)
    args = list(host[2])
    args[args.index("--global-lock") + 1] = str(directory / "guards.lock")
    result = run((host[0], host[1], args), "begin", "--operation-id", OP, "--identity", IDENTITY)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux inherited proof")
def test_stopped_finish_requires_own_continuous_leases_and_prior_drain(host):
    hold(host)
    with open(host[0] / "global.lock", "rb") as global_fd, host[1].exclusive_lease() as admission_fd:
        fcntl.flock(global_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        flags = ["--global-fd", str(global_fd.fileno()), "--admission-fd", str(admission_fd)]
        fds = (global_fd.fileno(), admission_fd)
        assert run(host, "verify-ack", "--operation-id", OP, "--identity", IDENTITY, *flags, fds=fds).returncode == 0
        (host[0] / "systemd").write_text("ActiveState=inactive\nMainPID=0\nControlGroup=\nSlice=legaltech.slice\nResult=success\n")
        (host[0] / "proc/512/stat").unlink()
        (host[0] / "ack/ack.json").unlink()
        (host[0] / "ack").rmdir()
        result = run(host, "verify-ack", "--stopped", "--operation-id", OP, "--identity", IDENTITY, *flags, fds=fds)
        assert result.returncode == 0, result.stderr
        assert host[1].read_control().state == "hold"
        result = run(host, "finish", "--stopped", "--operation-id", OP, "--identity", IDENTITY, *flags, fds=fds)
        assert result.returncode == 0, result.stderr
    assert host[1].read_control().state == "open"
