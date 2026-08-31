#!/usr/bin/env python3
"""Stopped-only first hold and authenticated adoption. No service lifecycle."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import grp
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Callable
import uuid
from zoneinfo import ZoneInfo


def load_sibling(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


operator = load_sibling("bootstrap_operator", "worker-maintenance.py")
audit = load_sibling("bootstrap_auditor", "bootstrap-audit.py")
MaintenanceError = operator.MaintenanceError
require = operator.require
UNITS = ("estrado-pjud.service", "estrado-pjud-worker.service")
PROPERTIES = ("LoadState", "FragmentPath", "DropInPaths", "NeedDaemonReload", "UnitFileState",
              "ActiveState", "SubState", "Result", "MainPID", "ExecMainPID", "ExecMainCode",
              "ExecMainStatus", "ExecMainExitTimestampMonotonic", "ControlGroup", "Slice", "Job")


@dataclass
class Config:
    expected_sha: str
    repo_dir: Path = Path("/opt/legal-tech-microservices")
    systemd_dir: Path = Path("/etc/systemd/system")
    proc_root: Path = Path("/proc")
    cgroup_root: Path = Path("/sys/fs/cgroup")
    control_dir: Path = Path("/var/lib/worker-maintenance")
    ack_dir: Path = Path("/run/worker-maintenance")
    journal_root: Path = Path("/var/lib/worker-maintenance-operations")
    bootstrap_root: Path = Path("/var/lib/worker-maintenance-bootstrap")
    global_lock: Path = Path("/run/lock/legaltech-resource-guards.lock")
    root_uid: int = 0
    root_gid: int = 0
    worker_uid: int = 0
    worker_gid: int = 0
    clock: Callable = lambda: datetime.now(timezone.utc)


def window(config):
    now = config.clock()
    require(now.utcoffset() is not None)
    hour = now.astimezone(ZoneInfo("America/Santiago")).hour
    require(hour >= 20 or hour < 4)


def trusted_directory(config, path):
    value = operator.safe_path(path).stat(follow_symlinks=False)
    require(stat.S_ISDIR(value.st_mode) and value.st_uid == config.root_uid
            and stat.S_IMODE(value.st_mode) & 0o022 == 0)


def trusted_ancestors(config, path):
    """Root ancestors cannot be renamed by others; sticky /run/lock is valid."""
    path = operator.safe_path(path)
    for directory in (path, *path.parents):
        value = directory.stat(follow_symlinks=False)
        owners = {config.root_uid} if directory == path else {0, config.root_uid}
        require(stat.S_ISDIR(value.st_mode) and value.st_uid in owners
                and (stat.S_IMODE(value.st_mode) & 0o022 == 0
                     or stat.S_IMODE(value.st_mode) & 0o1000 != 0))


def file_text(config, path, mode=0o644):
    def policy(value):
        operator.metadata(value, config.root_uid, config.root_gid, mode)
    return audit.bounded_read(path, 65536, policy)


def absent(path):
    require(not operator.safe_path(path).exists() and not path.is_symlink())


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def trusted_git_file(config, path):
    value = operator.safe_path(path).lstat()
    require(stat.S_ISREG(value.st_mode) and value.st_uid == config.root_uid
            and value.st_nlink == 1 and stat.S_IMODE(value.st_mode) & 0o022 == 0)


def bound_git(config, runner):
    """Authenticate discovery, then compare only the fixed installed worktree."""
    trusted_directory(config, config.repo_dir)
    trusted_ancestors(config, config.repo_dir)
    marker = operator.safe_path(config.repo_dir / ".git")
    if marker.is_dir():
        trusted_directory(config, marker)
    else:
        trusted_git_file(config, marker)
    git = ["/usr/bin/git", "-c", "core.fsmonitor=false", "-C", str(config.repo_dir)]
    require(audit.command_output(runner, [*git, "rev-parse", "--show-toplevel"]).strip() == str(config.repo_dir))
    gitdir = Path(audit.command_output(runner, [*git, "rev-parse", "--absolute-git-dir"]).strip())
    common = Path(audit.command_output(runner, [*git, "rev-parse", "--path-format=absolute", "--git-common-dir"]).strip())
    for path in {gitdir, common}:
        trusted_directory(config, path)
        trusted_ancestors(config, path)
    for path in (gitdir / "HEAD", gitdir / "index", common / "config"):
        trusted_git_file(config, path)
    for path in (gitdir / "commondir", gitdir / "config.worktree"):
        if path.exists() or path.is_symlink():
            trusted_git_file(config, path)
    return ["/usr/bin/git", "-c", "core.fsmonitor=false",
            "--git-dir=" + str(gitdir), "--work-tree=" + str(config.repo_dir)]


def exact_tree(config, runner):
    require(re.fullmatch(r"[0-9a-f]{40}", config.expected_sha) is not None)
    git = bound_git(config, runner)
    audit.require_safe_git_comparison(runner, git)
    tags = audit.command_output(runner, [*git, "ls-files", "-v", "-z"])
    require(all(item.startswith("H ") for item in tags.split("\0") if item))
    require(audit.command_output(runner, [*git, "rev-parse", "HEAD"]).strip() == config.expected_sha)
    require(audit.command_output(runner, [*git, "status", "--porcelain=v1", "--untracked-files=all"]) == "")


@contextmanager
def global_ex(config):
    """Never creates/replaces the global lock, including a missing-lock race."""
    path = operator.safe_path(config.global_lock)
    trusted_ancestors(config, path.parent)
    named = path.lstat()
    operator.metadata(named, config.root_uid, config.root_gid, 0o600)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        operator.metadata(opened, config.root_uid, config.root_gid, 0o600)
        require((opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(path.lstat().st_ino == opened.st_ino)
        yield
    finally:
        os.close(fd)


def installed_files(config, runner):
    trusted_directory(config, config.systemd_dir)
    result = {}
    for unit in UNITS:
        raw = audit.command_output(runner, ["/usr/bin/systemctl", "show", unit,
                                  *("--property=" + key for key in PROPERTIES)])
        values = audit.unique_pairs(line.split("=", 1) for line in raw.splitlines())
        require(set(values) == set(PROPERTIES))
        require(values["LoadState"] == "loaded" and values["NeedDaemonReload"] == "no"
                and values["FragmentPath"] == str(config.systemd_dir / unit) and values["Job"] == "")
        file_text(config, config.systemd_dir / unit)
        drop = operator.safe_path(config.systemd_dir / (unit + ".d/xvfb.conf"))
        expected = str(drop) if unit == UNITS[1] and drop.exists() else ""
        require(values["DropInPaths"] == expected)
        if expected:
            trusted_directory(config, drop.parent)
            file_text(config, drop)
        result[unit] = values
    return result


def empty_cgroups(config):
    operator.safe_path(config.cgroup_root)
    require(config.cgroup_root.is_dir())
    groups = [f"/{slice_name}/{unit}" for slice_name in ("system.slice", "legaltech.slice") for unit in UNITS]
    for group in groups:
        path = operator.safe_path(config.cgroup_root / group.lstrip("/"))
        if path.exists():
            require(path.is_dir())
            events = audit.unique_pairs(line.split() for line in audit.bounded_read(path / "cgroup.events").splitlines())
            require(events.get("populated") == "0")
            for directory, children, _ in os.walk(path, followlinks=False):
                for child in children:
                    require(not (Path(directory) / child).is_symlink())
                require(audit.bounded_read(Path(directory) / "cgroup.procs").strip() == "")
    for process in config.proc_root.iterdir():
        if process.name.isdigit():
            try:
                membership = audit.bounded_read(process / "cgroup").strip()
            except FileNotFoundError:
                require(not process.exists())  # Only a process that disappeared may be skipped.
                continue
            for line in membership.splitlines():
                fields = line.split(":", 2)
                require(len(fields) == 3)
                require(not any(fields[2] == group or fields[2].startswith(group + "/") for group in groups))


def stopped_snapshot(config, runner):
    boot = audit.bounded_read(config.proc_root / "sys/kernel/random/boot_id").strip()
    require(str(uuid.UUID(boot)) == boot)
    services = installed_files(config, runner)
    for values in services.values():
        require(all(values[key] == expected for key, expected in {
            "UnitFileState": "disabled", "ActiveState": "inactive", "SubState": "dead",
            "Result": "success", "MainPID": "0", "ExecMainCode": "1", "ExecMainStatus": "0",
            "ControlGroup": "",
        }.items()))
        require(values["Slice"] in ("system.slice", "legaltech.slice"))
        require(re.fullmatch(r"[1-9][0-9]{0,9}", values["ExecMainPID"]) is not None)
        require(re.fullmatch(r"[1-9][0-9]{0,19}", values["ExecMainExitTimestampMonotonic"]) is not None)
        absent(config.proc_root / values["ExecMainPID"])
    empty_cgroups(config)
    require(audit.bounded_read(config.proc_root / "sys/kernel/random/boot_id").strip() == boot)
    return boot, services


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_directory(config, path, gid, mode):
    window(config)
    trusted_directory(config, path.parent)
    path.mkdir(mode=mode)
    os.chown(path, config.root_uid, gid)
    path.chmod(mode)
    sync_directory(path)
    sync_directory(path.parent)


def atomic_file(config, path, text, mode, *, create_only=False):
    window(config)
    trusted_directory(config, path.parent)
    operator.safe_path(path)
    if create_only:
        absent(path)
    elif path.exists():
        file_text(config, path, mode)
    fd, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=path.parent)
    try:
        os.fchown(fd, config.root_uid, config.root_gid)
        os.fchmod(fd, mode)
        data = memoryview(text.encode("utf-8"))
        while data:
            count = os.write(fd, data)
            require(count > 0)
            data = data[count:]
        os.fsync(fd)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def operator_args(config):
    drop = operator.safe_path(config.systemd_dir / (UNITS[1] + ".d/xvfb.conf"))
    return SimpleNamespace(root_uid=config.root_uid, root_gid=config.root_gid,
        unit_file=config.systemd_dir / UNITS[1], dropin_file=drop if drop.exists() else None,
        journal_root=config.journal_root, control_dir=config.control_dir, ack_dir=config.ack_dir,
        proc_root=config.proc_root, systemctl="/usr/bin/systemctl", timeout_seconds=10,
        health_url="http://127.0.0.1:8000/api/v1/health", test_mode=False,
        admission_fd=None, global_fd=None)


def store_for(config):
    return operator.MaintenanceStore(config.control_dir, config.ack_dir,
        operator.StorePolicy(config.root_uid, config.worker_gid, config.worker_uid, config.worker_gid),
        allow_control_writes=True, defer_ack=True)


def record_write(config, record):
    atomic_file(config, config.bootstrap_root / "record.json", json.dumps(record, sort_keys=True) + "\n", 0o600)


def install(config, runner=subprocess.run):
    """Caller must independently establish business/RPC closure before invoking."""
    window(config)
    with global_ex(config):
        exact_tree(config, runner)
        for path in (config.control_dir, config.ack_dir, config.journal_root, config.bootstrap_root):
            absent(path)
            trusted_directory(config, path.parent)
        first = stopped_snapshot(config, runner)
        original = file_text(config, config.systemd_dir / UNITS[1])
        target = target_unit(original)
        args = operator_args(config)
        drop = file_text(config, args.dropin_file) if args.dropin_file else None
        if drop is not None:
            # Existing operator accepts only this optional override; protect its keys before any write.
            values = service_values(drop)
            require(set(values) <= {"NotifyAccess", "Environment", "ExecStart"})
            require(values.get("ExecStart") == ["", "/usr/bin/xvfb-run -a /opt/legal-tech-microservices/estrado-pjud-service/.venv/bin/python -m worker"])
        exact_tree(config, runner)
        require(stopped_snapshot(config, runner) == first)
        require(file_text(config, args.unit_file) == original)
        require(args.dropin_file is None or file_text(config, args.dropin_file) == drop)
        window(config)
        operation = str(uuid.uuid4())
        record = dict(version=1, operation_id=operation, expected_sha=config.expected_sha,
                      original_hash=digest(original), target_hash=digest(target),
                      dropin_hash=digest(drop) if drop is not None else None, phase="prepared")
        create_directory(config, config.bootstrap_root, config.root_gid, 0o700)
        record_write(config, record)
        atomic_file(config, config.bootstrap_root / "worker-unit.original", original, 0o600, create_only=True)
        atomic_file(config, args.unit_file, target, 0o644)
        operator.validate_unit(args)
        record["phase"] = "unit_installed"
        record_write(config, record)
        create_directory(config, config.control_dir, config.worker_gid, 0o750)
        window(config)
        lock = config.control_dir / "admission.lock"
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        try:
            os.fchown(fd, config.root_uid, config.worker_gid)
            os.fchmod(fd, 0o640)
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_directory(config.control_dir)
        window(config)
        store_for(config).initialize_hold(operation)
        record["phase"] = "installed"
        record_write(config, record)
        return dict(operation_id=operation, phase="installed", result="succeeded")


def record_read(config, operation):
    operator.metadata(operator.safe_path(config.bootstrap_root).stat(), config.root_uid, config.root_gid, 0o700, True)
    value = json.loads(file_text(config, config.bootstrap_root / "record.json", 0o600),
                       object_pairs_hook=operator.unique_pairs)
    require(type(value) is dict and set(value) == {
        "version", "operation_id", "expected_sha", "original_hash", "target_hash", "dropin_hash", "phase"})
    require(type(value["version"]) is int and value["version"] == 1 and value["operation_id"] == operation
            and value["expected_sha"] == config.expected_sha and value["phase"] == "installed")
    original = file_text(config, config.bootstrap_root / "worker-unit.original", 0o600)
    target = target_unit(original)
    require(value["original_hash"] == digest(original) and value["target_hash"] == digest(target))
    require(file_text(config, config.systemd_dir / UNITS[1]) == target)
    drop = operator_args(config).dropin_file
    require(value["dropin_hash"] == (digest(file_text(config, drop)) if drop else None))
    return value


def adopt(config, operation, runner=subprocess.run):
    """First adoption only. A normal finish remains an independent operator action."""
    require(type(operation) is str and str(uuid.UUID(operation)) == operation)
    window(config)
    with global_ex(config):
        exact_tree(config, runner)
        record = record_read(config, operation)
        absent(config.journal_root)
        trusted_directory(config, config.journal_root.parent)
        first = installed_files(config, runner)
        args = operator_args(config)
        operator.validate_unit(args)
        store = store_for(config)
        control = store.read_control()
        require(control.state == "hold" and control.operation_id == operation)
        with operator.admission_lease(args, store):
            identity = operator.current_identity(args, store)
            ack = store.read_ack(expected_operation_id=operation, expected_identity=identity)
            require(ack.state == "quiescent" and ack.inflight == 0)
            operator.healthy(args)
            exact_tree(config, runner)
            require(installed_files(config, runner) == first)
            require(record_read(config, operation) == record)
            operator.current_identity(args, store, identity)
            ack = store.read_ack(expected_operation_id=operation, expected_identity=identity)
            require(ack.state == "quiescent" and ack.inflight == 0 and store.read_control() == control)
            absent(config.journal_root)
            window(config)
            text = operator.identity_text(identity)
            operator.journal_write(args, dict(version=1, operation_id=operation,
                initial_identity=text, drained_identity=text, result="intended"))
            record["phase"] = "adopted"
            record_write(config, record)
        return dict(operation_id=operation, phase="adopted", result="succeeded")


def service_values(raw):
    """Fail closed on ambiguous continuations/duplicate protected assignments."""
    require("\x00" not in raw and "\r" not in raw and "\\\n" not in raw)
    values = {}
    section = ""
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        require("=" in line)
        key, value = (part.strip() for part in line.split("=", 1))
        if section == "Service":
            values.setdefault(key, []).append(value)
    return values


def target_unit(original):
    values = service_values(original)
    require("RuntimeDirectory" not in values and "RuntimeDirectoryMode" not in values)
    for key, expected in {
        "Type": "notify", "User": "estrado", "Group": "estrado", "ProtectSystem": "strict",
        "WorkingDirectory": "/opt/legal-tech-microservices/estrado-pjud-service",
        "ExecStart": "/opt/legal-tech-microservices/estrado-pjud-service/.venv/bin/python -m worker",
    }.items():
        require(values.get(key) == [expected])
    for key in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost", "ExecCondition"):
        require(all(not item for item in values.get(key, [])))
    require(original.count("[Service]\n") == 1)
    return original.replace("[Service]\n", "[Service]\nRuntimeDirectory=worker-maintenance\nRuntimeDirectoryMode=0700\n", 1)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("install", "adopt"))
    result.add_argument("--expected-sha", required=True)
    result.add_argument("--operation-id")
    return result


def execute(config, command, operation, runner=subprocess.run):
    try:
        require(command in ("install", "adopt") and (operation is None if command == "install" else operation is not None))
        return install(config, runner) if command == "install" else adopt(config, operation, runner)
    except Exception:
        # Report only validated finite recovery hints. Never echo OS/provider data.
        result = dict(operation_id=None, phase="validation", result="blocked")
        try:
            operator.metadata(operator.safe_path(config.bootstrap_root).stat(), config.root_uid, config.root_gid, 0o700, True)
            result["phase"] = "partial"
            record = json.loads(file_text(config, config.bootstrap_root / "record.json", 0o600),
                                object_pairs_hook=operator.unique_pairs)
            operation = record["operation_id"]
            require(type(operation) is str and str(uuid.UUID(operation)) == operation)
            require(record["phase"] in ("prepared", "unit_installed", "installed", "adopted"))
            result.update(operation_id=operation, phase=record["phase"])
        except Exception:
            pass
        return result


def main(argv=None):
    args = parser().parse_args(argv)
    result = dict(operation_id=None, phase="validation", result="blocked")
    status = 2
    try:
        require(sys.platform == "linux" and os.geteuid() == 0)
        require(re.fullmatch(r"[0-9a-f]{40}", args.expected_sha) is not None)
        require((args.command == "install" and args.operation_id is None)
                or (args.command == "adopt" and type(args.operation_id) is str
                    and str(uuid.UUID(args.operation_id)) == args.operation_id))
        config = Config(args.expected_sha, worker_uid=pwd.getpwnam("estrado").pw_uid,
                        worker_gid=grp.getgrnam("estrado").gr_gid)
        result = execute(config, args.command, args.operation_id)
        status = 0 if result["result"] == "succeeded" else 1
    except Exception:
        pass
    try:
        print(json.dumps(result, sort_keys=True), flush=True)
    except OSError:
        sys.stdout = None
        return 1  # Persisted state may exist; do not retry based on missing output.
    return status


if __name__ == "__main__":
    raise SystemExit(main())
