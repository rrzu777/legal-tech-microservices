"""Finite checkout/record recovery states; no service lifecycle or remote calls."""
from contextlib import nullcontext
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def module():
    script = Path(__file__).resolve().parents[1] / "bootstrap-worker-handoff.py"
    spec = importlib.util.spec_from_file_location("handoff_test", script)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def test_handoff_entrypoint_exists(module):
    assert callable(getattr(module, "handoff", None))


@pytest.mark.parametrize("changed", ["estrado-pjud-service/worker/config.py", "ops/systemd/estrado-pjud.service", "requirements.txt", "ops/unreviewed.py"])
def test_runtime_or_unreviewed_path_cannot_be_handed_off(module, tmp_path, monkeypatch, changed):
    monkeypatch.setattr(module, "git", lambda config, runner, *args:
                        changed + "\0" if args[0] == "diff" else "")
    with pytest.raises(module.bootstrap.MaintenanceError):
        module.verify_delta(SimpleNamespace(), "a" * 40, "b" * 40)


def test_real_git_ops_only_delta_and_complete_runtime_trees(module, tmp_path, monkeypatch):
    def command(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True)
    command("init", "-q")
    for path in ("ops/resource-guards.sh", "estrado-pjud-service/worker/__main__.py", "ops/systemd/estrado-pjud.service"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("initial\n")
    command("add", ".")
    command("-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "old")
    previous = command("rev-parse", "HEAD").strip()
    (tmp_path / "ops/resource-guards.sh").write_text("updated ops\n")
    command("add", ".")
    command("-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "new")
    target = command("rev-parse", "HEAD").strip()
    monkeypatch.setattr(module, "git", lambda config, runner, *args: command(*args))
    result = module.verify_delta(SimpleNamespace(), previous, target)
    assert result["paths"] == ["ops/resource-guards.sh"]
    assert set(result["runtime_trees"]) == {"estrado-pjud-service", "ops/systemd"}


def test_real_git_rename_from_outside_allowlist_is_rejected(module, tmp_path, monkeypatch):
    def command(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True)
    command("init", "-q")
    command("config", "diff.renames", "true")
    for path in ("requirements.txt", "estrado-pjud-service/worker/__main__.py", "ops/systemd/estrado-pjud.service"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original contents\n")
    command("add", ".")
    command("-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "old")
    previous = command("rev-parse", "HEAD").strip()
    command("mv", "requirements.txt", "ops/README.md")
    command("-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "rename")
    target = command("rev-parse", "HEAD").strip()
    assert command("diff", "--name-status", previous, target).strip() == "R100\trequirements.txt\tops/README.md"
    assert command("diff", "--name-only", previous, target).strip() == "ops/README.md"
    monkeypatch.setattr(module, "git", lambda config, runner, *args: command(*args))
    with pytest.raises(module.bootstrap.MaintenanceError):
        module.verify_delta(SimpleNamespace(), previous, target)


@pytest.fixture
def transaction(module, tmp_path, monkeypatch):
    old, new = "a" * 40, "b" * 40
    operation = "64a8eb10-2d55-457f-924c-23d5a532c847"
    config = module.bootstrap.Config(new, repo_dir=tmp_path / "live", bootstrap_root=tmp_path / "bootstrap",
                                     root_uid=os.getuid(), root_gid=os.getgid())
    config.repo_dir.mkdir()
    config.bootstrap_root.mkdir(mode=0o700)
    stage = replace(config, repo_dir=tmp_path / "stage")
    stage.repo_dir.mkdir()
    record = dict(version=1, operation_id=operation, expected_sha=old, original_hash="1" * 64,
                  target_hash="2" * 64, dropin_hash=None, phase="adopted")
    record_path = config.bootstrap_root / "record.json"
    original = json.dumps(record, sort_keys=True) + "\n"
    record_path.write_text(original)
    state = SimpleNamespace(sha=old, fail_at=None, events=[], valid_hold=True, snapshot={"fixed": True})
    monkeypatch.setattr(module.bootstrap, "window", lambda config: None)
    monkeypatch.setattr(module, "trusted_toolkit", lambda config: None)
    monkeypatch.setattr(module.bootstrap, "trusted_directory", lambda *args: None)
    monkeypatch.setattr(module.bootstrap, "operator_args", lambda config: SimpleNamespace())
    monkeypatch.setattr(module.bootstrap, "store_for", lambda config: object())
    monkeypatch.setattr(module.bootstrap.operator, "global_lease", lambda args: nullcontext())
    monkeypatch.setattr(module.bootstrap.operator, "admission_lease", lambda args, store: nullcontext())
    def hold(*args):
        if not state.valid_hold:
            raise module.bootstrap.MaintenanceError()
    monkeypatch.setattr(module, "assert_current_hold", hold)
    monkeypatch.setattr(module, "immutable_snapshot", lambda *args: dict(state.snapshot))
    monkeypatch.setattr(module.bootstrap, "verify_adopted", lambda *args, **kwargs: state.events.append("phase-a"))
    def recovery(*args, **kwargs):
        assert kwargs["expected_sha"] == old
        state.events.append("recovery-verified")
    monkeypatch.setattr(module.bootstrap, "verify_recovery_backup", recovery, raising=False)
    monkeypatch.setattr(module.bootstrap.operator, "parse_identity", lambda value: value)
    def exact(cfg, runner):
        if cfg.repo_dir == config.repo_dir:
            assert cfg.expected_sha == state.sha
    monkeypatch.setattr(module.bootstrap, "exact_tree", exact)
    monkeypatch.setattr(module, "verify_delta", lambda *args: {"paths": ["ops/resource-guards.sh"], "runtime_trees": {}})
    def git(cfg, runner, *args):
        if args[:2] == ("rev-parse", "HEAD"):
            return state.sha
        assert "switch" in args and "core.hooksPath=/dev/null" in args
        assert "prepared" in (config.bootstrap_root / ("handoff-" + new) / "handoff.json").read_text()
        state.events.append("checkout")
        state.sha = args[-1]
        if state.fail_at == "checkout":
            raise RuntimeError("after checkout")
        return ""
    monkeypatch.setattr(module, "git", git)
    monkeypatch.setattr(module.bootstrap, "file_text", lambda cfg, path, mode=0o644: path.read_text())
    def read_record(cfg, op, phase):
        value = json.loads(record_path.read_text())
        assert value["expected_sha"] == cfg.expected_sha and value["operation_id"] == op
        return value
    monkeypatch.setattr(module.bootstrap, "record_read", read_record)
    def create_directory(cfg, path, gid, mode):
        path.mkdir(mode=mode)
        if state.fail_at == "directory":
            raise RuntimeError("after directory")
    monkeypatch.setattr(module.bootstrap, "create_directory", create_directory)
    def atomic(cfg, path, text, mode, create_only=False):
        if create_only:
            assert not path.exists()
        path.write_text(text)
        path.chmod(mode)
        if path.name == "record.original" and state.fail_at == "original":
            raise RuntimeError("after original")
        if path.name == "handoff.json" and state.fail_at == "prepared" and '"prepared"' in text:
            raise RuntimeError("after prepared")
        if path == record_path:
            state.events.append("record")
            if state.fail_at == "record":
                raise RuntimeError("after record")
    monkeypatch.setattr(module.bootstrap, "atomic_file", atomic)
    run = lambda: module.handoff(config, stage, old, operation, "identity", 8, 9, tmp_path / "backup")
    return SimpleNamespace(run=run, state=state, config=config, original=original,
                           record_path=record_path, old=old, new=new)


def test_checkout_then_record_commits_with_no_lifecycle(transaction):
    result = transaction.run()
    assert result["status"] == "handoff_committed"
    assert transaction.state.events.index("phase-a") < transaction.state.events.index("checkout")
    assert transaction.state.events.index("checkout") < transaction.state.events.index("record")
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["phase"] == "committed"
    assert (Path(result["receipt"]).parent / "record.original").read_text() == transaction.original
    assert json.loads(transaction.record_path.read_text())["expected_sha"] == transaction.new


@pytest.mark.parametrize("boundary", ["checkout", "record"])
def test_partial_handoff_can_resume_only_its_recorded_pair(transaction, boundary):
    transaction.state.fail_at = boundary
    with pytest.raises(RuntimeError):
        transaction.run()
    assert transaction.state.sha == transaction.new
    receipt_path = transaction.config.bootstrap_root / ("handoff-" + transaction.new) / "handoff.json"
    assert json.loads(receipt_path.read_text())["phase"] == "prepared"
    transaction.state.fail_at = None
    assert transaction.run()["status"] == "handoff_committed"
    assert transaction.state.events.count("checkout") == 1


@pytest.mark.parametrize("boundary", ["directory", "original", "prepared"])
def test_partial_preparation_preserves_old_pair_and_resumes(transaction, boundary):
    transaction.state.fail_at = boundary
    with pytest.raises(RuntimeError):
        transaction.run()
    assert transaction.state.sha == transaction.old
    assert transaction.record_path.read_text() == transaction.original
    transaction.state.fail_at = None
    assert transaction.run()["status"] == "handoff_committed"


def test_partial_directory_with_unknown_file_is_never_overwritten(transaction):
    transaction.state.fail_at = "directory"
    with pytest.raises(RuntimeError):
        transaction.run()
    directory = transaction.config.bootstrap_root / ("handoff-" + transaction.new)
    unknown = directory / "unknown"
    unknown.write_text("preserve")
    transaction.state.fail_at = None
    with pytest.raises(Exception):
        transaction.run()
    assert unknown.read_text() == "preserve"
    assert transaction.state.sha == transaction.old


def test_partial_state_with_changed_journal_stays_blocked(transaction):
    transaction.state.fail_at = "checkout"
    with pytest.raises(RuntimeError):
        transaction.run()
    transaction.state.fail_at = None
    transaction.state.snapshot["fixed"] = False
    with pytest.raises(Exception):
        transaction.run()
    assert transaction.record_path.read_text() == transaction.original


def test_open_control_never_changes_checkout_or_record(transaction):
    transaction.state.valid_hold = False
    with pytest.raises(Exception):
        transaction.run()
    assert transaction.state.sha == transaction.old
    assert transaction.record_path.read_text() == transaction.original


@pytest.mark.parametrize("fault", [None, "malformed", "operation", "sha"])
def test_real_handoff_then_bootstrap_cli_loads_receipt_fail_closed(module, tmp_path, monkeypatch, capsys, fault):
    """Actual A/B loaders, Git checkout, record/receipt fsync and held files.

    Synthetic host only: systemctl/kernel and rollback comparison use A's host
    boundary. CLI default paths/root account are injected, never production.
    """
    import fcntl
    import shutil
    import test_worker_bootstrap as fixtures

    real_run = subprocess.run
    bootstrap = module.bootstrap
    host = fixtures.host.__wrapped__(bootstrap, tmp_path)
    fixtures.closed_worker.__wrapped__(host, monkeypatch)
    fixtures.rolled_back_worker.__wrapped__(host)
    source = Path(__file__).resolve().parents[2]
    for relative in (
        "ops/bootstrap-worker-handoff.py", "ops/bootstrap-worker-maintenance.py",
        "ops/bootstrap-audit.py", "ops/worker-maintenance.py", "ops/worker-maintenance.sh",
        "ops/resource-guards.sh", "ops/resource-guards-rollback.py",
        "ops/systemd/estrado-pjud-worker.service",
        "estrado-pjud-service/worker/__init__.py", "estrado-pjud-service/worker/maintenance_store.py",
    ):
        destination = host.config.repo_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    def command(*args):
        return real_run(["/usr/bin/git", "-C", str(host.config.repo_dir), *args], env=env,
                        check=True, capture_output=True, text=True).stdout.strip()
    command("add", ".")
    command("-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "old")
    old = command("rev-parse", "HEAD")
    guard = host.config.repo_dir / "ops/resource-guards.sh"
    guard.write_text(guard.read_text() + "\n# fixture ops-only update\n")
    command("add", ".")
    command("-c", "user.name=fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "target")
    new = command("rev-parse", "HEAD")
    stage_dir = tmp_path / "stage"
    real_run(["/usr/bin/git", "clone", "--quiet", "--no-hardlinks", str(host.config.repo_dir), str(stage_dir)],
             env=env, check=True, capture_output=True)
    command("switch", "--detach", old)
    config = replace(host.config, expected_sha=new)
    stage = replace(config, repo_dir=stage_dir)
    record_path = config.bootstrap_root / "record.json"
    record = json.loads(record_path.read_text())
    record["expected_sha"] = old
    record_path.write_text(json.dumps(record, sort_keys=True) + "\n")
    (host.backup / "expected-sha").write_text(old + "\n")
    boundary_runner = host.runner
    def runner(command, **kwargs):
        if command[0] == "/usr/bin/git":
            return real_run(command, **kwargs)
        return boundary_runner(command, **kwargs)
    monkeypatch.setattr(subprocess, "run", runner)
    # macOS cannot inspect Linux fdinfo; the descriptor inode, metadata and
    # flock are real. Linux root coverage independently validates fdinfo.
    def fd_check(fd, path, uid, gid, mode):
        opened, named = os.fstat(fd), os.lstat(path)
        bootstrap.operator.metadata(opened, uid, gid, mode)
        bootstrap.operator.metadata(named, uid, gid, mode)
        assert (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
    monkeypatch.setattr(bootstrap.operator, "validate_held_fd", fd_check)
    gfd = os.open(config.global_lock, os.O_RDWR | os.O_NOFOLLOW)
    afd = os.open(config.control_dir / "admission.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(gfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(afd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = bootstrap.operator.identity_text(host.identity)
        result = module.handoff(config, stage, old, host.operation, identity, gfd, afd, host.backup, runner)
        receipt_path = Path(result["receipt"])
        assert command("rev-parse", "HEAD") == new
        assert command("status", "--porcelain") == ""
        assert host.store.read_control().state == "hold"
        original_record = record_path.read_bytes()
        if fault == "malformed":
            receipt_path.write_text("{broken")
        elif fault:
            receipt = json.loads(receipt_path.read_text())
            receipt["operation_id" if fault == "operation" else "target_sha"] = (
                "93a0cdd4-08eb-48d7-b6b4-42b3ef6b306b" if fault == "operation" else "c" * 40)
            receipt_path.write_text(json.dumps(receipt))
        # Run the genuine parser/main/execute/verify-adopted -> dynamically
        # loaded B verifier -> fresh bootstrap sibling path, not a mock verifier.
        actual_execute = bootstrap.execute
        monkeypatch.setattr(bootstrap, "execute", lambda *args, **kwargs: actual_execute(*args, runner=runner, **kwargs))
        monkeypatch.setattr(bootstrap, "Config", lambda *args, **kwargs: config)
        monkeypatch.setattr(bootstrap.sys, "platform", "linux")
        monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
        monkeypatch.setattr(bootstrap.pwd, "getpwnam", lambda value: SimpleNamespace(pw_uid=os.getuid()))
        monkeypatch.setattr(bootstrap.grp, "getgrnam", lambda value: SimpleNamespace(gr_gid=os.getgid()))
        rc = bootstrap.main(["verify-adopted", "--expected-sha", new,
            "--operation-id", host.operation, "--identity", identity,
            "--global-fd", str(gfd), "--admission-fd", str(afd),
            "--recovery-backup", str(host.backup), "--handoff-receipt", str(receipt_path)])
        response = json.loads(capsys.readouterr().out)
        assert (rc, response["result"]) == ((0, "verified") if fault is None else (1, "blocked"))
        assert record_path.read_bytes() == original_record
        assert host.store.read_control().state == "hold"
        assert command("status", "--porcelain") == ""
    finally:
        os.close(afd)
        os.close(gfd)
