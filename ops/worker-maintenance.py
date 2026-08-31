#!/usr/bin/env python3
"""Root-only v1 operator. No retries, bootstrap, proxy or worker-stop capability.

The shell mutator owns both descriptors continuously. A standalone begin/finish
owns its locks only for that command; durable hold survives command/process exit.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estrado-pjud-service"))
from worker.maintenance_store import Control, MaintenanceError, MaintenanceStore, ProcessIdentity, StorePolicy


def require(condition):
    if not condition:
        raise MaintenanceError()


def identity_text(identity):
    return f"{identity.boot_id}:{identity.pid}:{identity.start_ticks}:{identity.instance_id}"


def parse_identity(value):
    require(type(value) is str)
    boot, pid, ticks, instance = value.split(":")
    return ProcessIdentity(boot, int(pid), int(ticks), instance)


def safe_path(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts)
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        require(not current.is_symlink())
    return path


def metadata(value, uid, gid, mode, directory=False):
    require((stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
            and value.st_uid == uid and value.st_gid == gid
            and stat.S_IMODE(value.st_mode) == mode
            and (directory or value.st_nlink == 1))


def bounded_read(path, maximum=8192):
    fd = os.open(safe_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode))
        value = os.read(fd, maximum + 1)
        require(len(value) <= maximum)
        return value.decode("utf-8")
    finally:
        os.close(fd)


def validate_held_fd(fd, path, uid, gid, mode):
    """fdinfo authenticates WRITE flock on THIS description, never acquires it.

    Do not use flock -u on inherited descriptors: that releases the parent's
    lease. Independent-open contention alone would not authenticate its owner.
    """
    named = os.lstat(safe_path(path))
    opened = os.fstat(fd)
    metadata(named, uid, gid, mode)
    metadata(opened, uid, gid, mode)
    require((named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino))
    # Linux is the production platform; test proc-root never redirects fdinfo.
    info = bounded_read(f"/proc/{os.getpid()}/fdinfo/{fd}")
    locks = [line.split() for line in info.splitlines() if line.startswith("lock:")]
    require(len(locks) == 1 and locks[0][2:5] == ["FLOCK", "ADVISORY", "WRITE"])


@contextmanager
def global_lease(args):
    path = safe_path(args.global_lock)
    if args.global_fd is not None:
        validate_held_fd(args.global_fd, path, args.root_uid, args.root_gid, 0o600)
        yield
        return
    parent = path.parent
    parent_stat = parent.stat()
    require(stat.S_ISDIR(parent_stat.st_mode) and parent_stat.st_uid == args.root_uid
            and (stat.S_IMODE(parent_stat.st_mode) & 0o022 == 0
                 or stat.S_IMODE(parent_stat.st_mode) & 0o1000 != 0))
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata(os.fstat(fd), args.root_uid, args.root_gid, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = path.stat(follow_symlinks=False)
        require((named.st_dev, named.st_ino) == (os.fstat(fd).st_dev, os.fstat(fd).st_ino))
        yield
    finally:
        os.close(fd)


@contextmanager
def admission_lease(args, store):
    if args.admission_fd is not None:
        validate_held_fd(args.admission_fd, Path(args.control_dir) / "admission.lock",
                         store.policy.control_uid, store.policy.control_gid, 0o640)
        yield
    else:
        with store.exclusive_lease():
            yield


def current_identity(args, store, expected=None):
    values = worker_runtime(args)
    require(values["ActiveState"] == "active" and values["Result"] == "success"
            and values["Slice"] in ("legaltech.slice", "system.slice")
            and values["ControlGroup"] == f'/{values["Slice"]}/estrado-pjud-worker.service')
    pid = int(values["MainPID"])
    require(pid > 0)
    candidate = store.read_ack_candidate()
    identity = kernel_identity(args, pid, candidate.instance_id)
    cgroup = bounded_read(Path(args.proc_root) / str(pid) / "cgroup").strip()
    require(cgroup == f'0::{values["ControlGroup"]}')
    require(expected is None or identity == expected)
    store.read_ack(expected_operation_id=store.read_control().operation_id, expected_identity=identity)
    return identity


def worker_runtime(args):
    result = subprocess.run([args.systemctl, "show", "estrado-pjud-worker.service",
                             "--property=ActiveState", "--property=MainPID", "--property=ControlGroup",
                             "--property=Slice", "--property=Result"], capture_output=True, timeout=args.timeout_seconds, check=True)
    require(len(result.stdout) <= 8192)
    values = {}
    for line in result.stdout.decode("ascii").splitlines():
        key, value = line.split("=", 1)
        require(key not in values)
        values[key] = value
    require(set(values) == {"ActiveState", "MainPID", "ControlGroup", "Slice", "Result"})
    return values


def kernel_identity(args, pid, instance_id):
    if args.test_mode:
        boot = bounded_read(Path(args.proc_root) / "sys/kernel/random/boot_id").strip()
        process_stat = bounded_read(Path(args.proc_root) / str(pid) / "stat")
        require(process_stat.startswith(f"{pid} ("))
        ticks = int(process_stat.rsplit(")", 1)[1].split()[19])
        return ProcessIdentity(boot, pid, ticks, instance_id)
    else:
        return ProcessIdentity.for_pid(pid, instance_id)


def stopped_after_drain(args, journal, expected):
    require(args.global_fd is not None and args.admission_fd is not None and expected is not None
            and journal["drained_identity"] == identity_text(expected))
    values = worker_runtime(args)
    require(values == dict(ActiveState="inactive", MainPID="0", ControlGroup="",
                           Slice=values["Slice"], Result="success")
            and values["Slice"] in ("legaltech.slice", "system.slice"))
    # No PID0/bootstrap inference: a previously authenticated/drained instance
    # must no longer exist, while the owning shell has retained both EX leases.
    path = Path(args.proc_root) / str(expected.pid) / "stat"
    if path.exists():
        require(kernel_identity(args, expected.pid, expected.instance_id) != expected)
    else:
        require(not path.is_symlink())


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def validate_unit(args):
    """Only compatible worker launch/runtime may be installed or restored.

    This validates the files actually about to be restored, not their filenames.
    Live identity/ACK and EX remain the separate runtime authority.
    """
    protected = {"Type", "User", "Group", "WorkingDirectory", "RuntimeDirectory", "RuntimeDirectoryMode", "ProtectSystem"}
    values = {}
    exec_start = ""
    for source in (args.unit_file, args.dropin_file):
        if source is None:
            continue
        path = safe_path(source)
        metadata(path.stat(follow_symlinks=False), args.root_uid, args.root_gid, 0o644)
        section = ""
        for raw in bounded_read(path, 65536).splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if section != "Service":
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key in protected:
                require(key not in values)
                values[key] = value
            if key in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost", "ExecCondition"):
                require(not value)
            if key == "ExecStart":
                require(not value or not exec_start)
                exec_start = value
    require(values == dict(Type="notify", User="estrado", Group="estrado", ProtectSystem="strict",
                           WorkingDirectory="/opt/legal-tech-microservices/estrado-pjud-service",
                           RuntimeDirectory="worker-maintenance", RuntimeDirectoryMode="0700"))
    command = "/opt/legal-tech-microservices/estrado-pjud-service/.venv/bin/python -m worker"
    require(exec_start in (command, "/usr/bin/xvfb-run -a " + command))


def journal_read(args, operation):
    root = safe_path(args.journal_root)
    metadata(root.stat(), args.root_uid, args.root_gid, 0o700, True)
    path = root / f"{operation}.json"
    metadata(path.stat(follow_symlinks=False), args.root_uid, args.root_gid, 0o600)
    value = json.loads(bounded_read(path), object_pairs_hook=unique_pairs)
    require(type(value) is dict and set(value) == {"version", "operation_id", "initial_identity", "drained_identity", "result"}
            and type(value["version"]) is int and value["version"] == 1
            and value["operation_id"] == operation and value["result"] in ("intended", "succeeded"))
    parse_identity(value["initial_identity"])
    if value["drained_identity"] is not None:
        parse_identity(value["drained_identity"])
    return value


def journal_write(args, value):
    root = safe_path(args.journal_root)
    if not root.exists():
        parent = root.parent.stat()
        require(parent.st_uid == args.root_uid and stat.S_IMODE(parent.st_mode) & 0o022 == 0)
        root.mkdir(mode=0o700)
        os.chown(root, args.root_uid, args.root_gid)
        parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    metadata(root.stat(), args.root_uid, args.root_gid, 0o700, True)
    path = root / f'{value["operation_id"]}.json'
    if path.exists() or path.is_symlink():
        metadata(path.stat(follow_symlinks=False), args.root_uid, args.root_gid, 0o600)
    fd, temporary = tempfile.mkstemp(prefix=".operation-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, args.root_uid, args.root_gid)
        data = (json.dumps(value, sort_keys=True) + "\n").encode()
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
        os.replace(temporary, path)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def healthy(args):
    result = subprocess.run([args.systemctl, "is-active", "--quiet", "estrado-pjud.service"],
                            capture_output=True, timeout=10)
    require(result.returncode == 0)
    with urllib.request.urlopen(args.health_url, timeout=10) as response:
        require(response.status == 200 or (args.test_mode and args.health_url.startswith("file:")))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--test-mode", action="store_true")
    for field in ("control-dir", "ack-dir", "proc-root", "systemctl", "global-lock", "journal-root", "health-url"):
        p.add_argument("--" + field)
    for field in ("root-uid", "root-gid", "worker-uid", "worker-gid"):
        p.add_argument("--" + field, type=int)
    p.add_argument("command", choices=("status", "begin", "verify-ack", "finish"))
    p.add_argument("--operation-id")
    p.add_argument("--identity")
    p.add_argument("--global-fd", type=int)
    p.add_argument("--admission-fd", type=int)
    p.add_argument("--require-open", action="store_true")
    p.add_argument("--check-lock", action="store_true")
    p.add_argument("--check-lock-path", action="store_true")
    p.add_argument("--delegated", action="store_true")
    p.add_argument("--new-instance-from")
    p.add_argument("--timeout-seconds", type=int, default=10)
    p.add_argument("--stopped", action="store_true")
    p.add_argument("--unit-file")
    p.add_argument("--dropin-file")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    overrides = (args.control_dir, args.ack_dir, args.proc_root, args.systemctl, args.global_lock,
                 args.journal_root, args.health_url, args.root_uid, args.root_gid, args.worker_uid, args.worker_gid)
    if (args.test_mode and any(value is None for value in overrides)) or (not args.test_mode and any(value is not None for value in overrides)):
        p.error("test overrides require the complete explicit test boundary")
    try:
        require(1 <= args.timeout_seconds <= 10)
        if not args.test_mode:
            require(os.geteuid() == 0)
            args.control_dir = "/var/lib/worker-maintenance"
            args.ack_dir = "/run/worker-maintenance"
            args.proc_root = "/proc"
            args.systemctl = "/usr/bin/systemctl"
            args.global_lock = "/run/lock/legaltech-resource-guards.lock"
            args.journal_root = "/var/lib/worker-maintenance-operations"
            args.health_url = "http://127.0.0.1:8000/api/v1/health"
            args.root_uid = args.root_gid = 0
        if args.check_lock_path:
            require(args.command == "status")
            metadata(os.lstat(safe_path(args.global_lock)), args.root_uid, args.root_gid, 0o600)
            return 0
        if args.check_lock:
            require(args.command == "status" and args.global_fd is not None)
            validate_held_fd(args.global_fd, args.global_lock, args.root_uid, args.root_gid, 0o600)
            return 0
        if args.unit_file:
            require(args.command == "status")
            validate_unit(args)
            return 0
        store = (MaintenanceStore(args.control_dir, args.ack_dir,
                                  StorePolicy(args.root_uid, args.worker_gid, args.worker_uid, args.worker_gid),
                                  allow_control_writes=True, defer_ack=True)
                 if args.test_mode else MaintenanceStore.production(operator=True))
        if args.operation_id:
            require(str(uuid.UUID(args.operation_id)) == args.operation_id)
        expected = parse_identity(args.identity) if args.identity else None
        if args.command == "status" and not args.delegated:
            control = store.read_control()
            require(not args.require_open or control.state == "open")
            identity = current_identity(args, store, expected)
            print(control.state, control.operation_id, identity_text(identity))
            return 0
        with global_lease(args):
            control = store.read_control()
            if args.delegated:
                require(args.command == "status" and args.global_fd is not None and args.admission_fd is not None
                        and args.operation_id == control.operation_id and control.state == "hold")
                with admission_lease(args, store):
                    journal = journal_read(args, args.operation_id)
                    require(expected is not None and journal["drained_identity"] == identity_text(expected))
                return 0
            require(args.operation_id is not None)
            if args.command == "begin":
                require(control.state == "open" and control.operation_id != args.operation_id and expected is not None)
                identity = current_identity(args, store, expected)
                journal_write(args, dict(version=1, operation_id=args.operation_id,
                                         initial_identity=identity_text(identity), drained_identity=None, result="intended"))
                store.transition(control.operation_id, "open", Control(1, "hold", args.operation_id,
                                 datetime.now(timezone.utc).isoformat()))
                print(args.operation_id)
                return 0
            journal = journal_read(args, args.operation_id)
            require(control.operation_id == args.operation_id)
            if args.command == "finish" and control.state == "open":
                require(journal["result"] == "succeeded")
                return 0
            require(control.state == "hold")
            with admission_lease(args, store):
                if args.stopped:
                    require(args.command in ("verify-ack", "finish"))
                    stopped_after_drain(args, journal, expected)
                    identity = expected
                elif args.new_instance_from:
                    previous = parse_identity(args.new_instance_from)
                    identity = current_identity(args, store)
                    require(identity.instance_id != previous.instance_id and
                            (identity.boot_id, identity.pid, identity.start_ticks) !=
                            (previous.boot_id, previous.pid, previous.start_ticks))
                else:
                    require(expected is not None)
                    identity = current_identity(args, store, expected)
                if not args.stopped:
                    acknowledgement = store.read_ack(expected_operation_id=args.operation_id, expected_identity=identity)
                    require(acknowledgement.state == "quiescent" and acknowledgement.inflight == 0)
                    journal["drained_identity"] = identity_text(identity)
                    journal_write(args, journal)
                if args.command == "finish":
                    healthy(args)
                    journal["result"] = "succeeded"
                    journal_write(args, journal)
                    try:
                        store.transition(args.operation_id, "hold", Control(1, "open", args.operation_id,
                                         datetime.now(timezone.utc).isoformat()))
                    except (MaintenanceError, OSError):
                        print("ERROR: finalization uncertain; admission may already be open; do not retry mutation", file=sys.stderr)
                        return 3
                print(identity_text(identity))
                return 0
    except (MaintenanceError, OSError, ValueError, KeyError, IndexError, TypeError,
            subprocess.SubprocessError, UnicodeError, RecursionError):
        print("ERROR: maintenance protocol unavailable; hold is not released", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
