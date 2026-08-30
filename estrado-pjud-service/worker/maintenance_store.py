"""Fail-closed, local maintenance protocol. No application or environment imports.

Provision directories and the stable lock while the worker is stopped. Control
writers must own the guards' global mutation lock throughout read/CAS/side effects;
the admission lock is deliberately separate so hold can drain existing readers.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import stat
from typing import Iterator
import uuid


MAX_JSON_BYTES = 8192
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_DIRECTORY


class MaintenanceError(Exception):
    """Contains only an allowlisted diagnostic, never file contents or OS errors."""

    def __init__(self) -> None:
        super().__init__("maintenance protocol unavailable")


class AdmissionClosed(MaintenanceError):
    def __init__(self) -> None:
        Exception.__init__(self, "maintenance admission closed")


def _require(condition: bool) -> None:
    if not condition:
        raise MaintenanceError()


def _canonical_uuid(value: str) -> None:
    _require(type(value) is str)
    try:
        _require(str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError):
        raise MaintenanceError() from None


def _integer(value: int, minimum: int) -> None:
    _require(type(value) is int and minimum <= value <= (2**63 - 1))


def _utc_datetime(value: str) -> None:
    _require(type(value) is str and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value
    ) is not None)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(parsed.utcoffset() == timedelta(0))
    except ValueError:
        raise MaintenanceError() from None


@dataclass(frozen=True)
class Control:
    version: int
    state: str
    operation_id: str
    created_at: str

    def __post_init__(self) -> None:
        _require(type(self.version) is int and self.version == 1)
        _require(type(self.state) is str and self.state in ("open", "hold"))
        _canonical_uuid(self.operation_id)
        _utc_datetime(self.created_at)


@dataclass(frozen=True)
class ProcessIdentity:
    boot_id: str
    pid: int
    start_ticks: int
    instance_id: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.boot_id)
        _integer(self.pid, 1)
        _integer(self.start_ticks, 1)
        _canonical_uuid(self.instance_id)

    @classmethod
    def current(cls) -> ProcessIdentity:
        """Fresh instance nonce and Linux boot/PID/start-time identity."""
        return cls.for_pid(os.getpid(), str(uuid.uuid4()))

    @classmethod
    def for_pid(cls, pid: int, instance_id: str) -> ProcessIdentity:
        """Read kernel identity; the caller authenticates MainPID/cgroup separately."""
        _integer(pid, 1)
        _canonical_uuid(instance_id)
        try:
            def read(path: str) -> str:
                fd = os.open(path, _READ_FLAGS)
                try:
                    raw = os.read(fd, MAX_JSON_BYTES + 1)
                    _require(len(raw) <= MAX_JSON_BYTES)
                    return raw.decode("ascii").strip()
                finally:
                    os.close(fd)
            boot_id = read("/proc/sys/kernel/random/boot_id")
            process_stat = read(f"/proc/{pid}/stat")
            _require(process_stat.startswith(f"{pid} ("))
            # comm may contain spaces and parentheses; field 22 follows its last ')'.
            start_ticks = int(process_stat.rsplit(")", 1)[1].split()[19])
            return cls(boot_id, pid, start_ticks, instance_id)
        except (OSError, ValueError, IndexError, UnicodeError):
            raise MaintenanceError() from None


@dataclass(frozen=True)
class Ack:
    version: int
    operation_id: str
    boot_id: str
    pid: int
    start_ticks: int
    instance_id: str
    state: str
    inflight: int

    def __post_init__(self) -> None:
        _require(type(self.version) is int and self.version == 1)
        _canonical_uuid(self.operation_id)
        ProcessIdentity(self.boot_id, self.pid, self.start_ticks, self.instance_id)
        _require(type(self.state) is str and self.state in ("draining", "quiescent"))
        _integer(self.inflight, 0)
        _require(self.state != "quiescent" or self.inflight == 0)


@dataclass(frozen=True)
class StorePolicy:
    control_uid: int
    control_gid: int
    ack_uid: int
    ack_gid: int

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            _integer(value, 0)


def _inode(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _snapshot(metadata: os.stat_result) -> tuple:
    return (_inode(metadata), metadata.st_mode, metadata.st_uid, metadata.st_gid,
            metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _object_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _decode(raw: bytes, schema):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(MaintenanceError()))
        _require(type(value) is dict and set(value) == {field.name for field in fields(schema)})
        return schema(**value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise MaintenanceError() from None


class MaintenanceStore:
    """Pinned directory/lock identities; all file IO is relative to verified dirfds.

    Injection is explicit for isolated local tests, never selected from environment.
    No constructor or reader creates resources. Control writes default to disabled.
    """

    def __init__(self, control_dir: str | Path, ack_dir: str | Path, policy: StorePolicy,
                 *, allow_control_writes: bool = False) -> None:
        _require(type(policy) is StorePolicy and type(allow_control_writes) is bool)
        self.policy = policy
        self._writable = allow_control_writes
        self._paths = {"control": Path(control_dir), "ack": Path(ack_dir)}
        self._directory_ids: dict[str, tuple[int, int]] = {}
        self._lock_id = None
        for kind in self._paths:
            path = self._paths[kind]
            _require(path.is_absolute() and ".." not in path.parts)
            with self._directory(kind) as fd:
                self._directory_ids[kind] = _inode(os.fstat(fd))
        with self._directory("control") as directory:
            with self._file(directory, "admission.lock", "control") as fd:
                self._lock_id = _inode(os.fstat(fd))

    @classmethod
    def production(cls, *, operator: bool = False) -> MaintenanceStore:
        """Fixed root/estrado authority and paths; operator is explicit root-only."""
        _require(type(operator) is bool)
        try:
            estrado = pwd.getpwnam("estrado")
            estrado_gid = grp.getgrnam("estrado").gr_gid
            _require(not operator or os.geteuid() == 0)
            return cls("/var/lib/worker-maintenance", "/run/worker-maintenance",
                       StorePolicy(0, estrado_gid, estrado.pw_uid, estrado_gid),
                       allow_control_writes=operator)
        except (KeyError, OSError):
            raise MaintenanceError() from None

    def _metadata(self, kind: str) -> tuple[int, int, int, int]:
        if kind == "control":
            return self.policy.control_uid, self.policy.control_gid, 0o750, 0o640
        return self.policy.ack_uid, self.policy.ack_gid, 0o700, 0o600

    @contextmanager
    def _directory(self, kind: str) -> Iterator[int]:
        """Walk every component without following symlinks, then pin leaf identity."""
        fd = None
        try:
            fd = os.open("/", _DIRECTORY_FLAGS)
            for component in self._paths[kind].parts[1:]:
                before = os.stat(component, dir_fd=fd, follow_symlinks=False)
                _require(stat.S_ISDIR(before.st_mode))
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
                try:
                    _require(_inode(before) == _inode(os.fstat(child)))
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
            metadata = os.fstat(fd)
            uid, gid, mode, _ = self._metadata(kind)
            _require(metadata.st_uid == uid and metadata.st_gid == gid
                     and stat.S_IMODE(metadata.st_mode) == mode)
            previous = self._directory_ids.get(kind)
            _require(previous is None or previous == _inode(metadata))
        except (OSError, ValueError):
            if fd is not None:
                os.close(fd)
            raise MaintenanceError() from None
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        try:
            yield fd
        finally:
            os.close(fd)

    def _validate_file(self, metadata: os.stat_result, kind: str) -> None:
        uid, gid, _, mode = self._metadata(kind)
        _require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
                 and metadata.st_uid == uid and metadata.st_gid == gid
                 and stat.S_IMODE(metadata.st_mode) == mode)

    @contextmanager
    def _file(self, directory: int, name: str, kind: str) -> Iterator[int]:
        fd = None
        try:
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            self._validate_file(before, kind)
            fd = os.open(name, _READ_FLAGS, dir_fd=directory)
            after = os.fstat(fd)
            self._validate_file(after, kind)
            _require(_snapshot(before) == _snapshot(after))
            if name == "admission.lock":
                _require(self._lock_id is None or self._lock_id == _inode(after))
        except OSError:
            if fd is not None:
                os.close(fd)
            raise MaintenanceError() from None
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        try:
            yield fd
        finally:
            os.close(fd)

    def _check_lock(self) -> None:
        with self._directory("control") as directory:
            with self._file(directory, "admission.lock", "control"):
                pass

    def _read(self, kind: str, name: str, schema):
        try:
            with self._directory(kind) as directory:
                with self._file(directory, name, kind) as fd:
                    before = os.fstat(fd)
                    _require(before.st_size <= MAX_JSON_BYTES)
                    raw = bytearray()
                    while len(raw) <= MAX_JSON_BYTES:
                        chunk = os.read(fd, MAX_JSON_BYTES + 1 - len(raw))
                        if not chunk:
                            break
                        raw.extend(chunk)
                    _require(len(raw) <= MAX_JSON_BYTES)
                    after = os.fstat(fd)
                    named = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    _require(_snapshot(before) == _snapshot(after) == _snapshot(named))
                    with self._directory(kind) as current:
                        _require(_inode(os.fstat(current)) == _inode(os.fstat(directory)))
                    return _decode(bytes(raw), schema)
        except OSError:
            raise MaintenanceError() from None

    def read_control(self) -> Control:
        self._check_lock()
        return self._read("control", "control.json", Control)

    def _existing(self, directory: int, name: str, kind: str):
        try:
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        self._validate_file(metadata, kind)
        with self._file(directory, name, kind) as fd:
            _require(_snapshot(metadata) == _snapshot(os.fstat(fd)))
        return _snapshot(metadata)

    def _write(self, kind: str, name: str, value, *, create_only: bool = False) -> None:
        payload = json.dumps(asdict(value), separators=(",", ":"), sort_keys=True).encode("utf-8")
        _require(len(payload) <= MAX_JSON_BYTES)
        try:
            with self._directory(kind) as directory:
                previous = self._existing(directory, name, kind)
                _require(not create_only or previous is None)
                temporary = f".{name}.{uuid.uuid4()}.tmp"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | os.O_CLOEXEC, 0o600, dir_fd=directory)
                try:
                    uid, gid, _, mode = self._metadata(kind)
                    os.fchown(fd, uid, gid)
                    os.fchmod(fd, mode)
                    remaining = memoryview(payload)
                    while remaining:
                        count = os.write(fd, remaining)
                        _require(count > 0)
                        remaining = remaining[count:]
                    os.fsync(fd)
                    self._validate_file(os.fstat(fd), kind)
                    _require(self._existing(directory, name, kind) == previous)
                    with self._directory(kind) as current:
                        _require(_inode(os.fstat(current)) == _inode(os.fstat(directory)))
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                    with self._file(directory, name, kind) as replaced:
                        _require(_inode(os.fstat(replaced)) == _inode(os.fstat(fd)))
                finally:
                    os.close(fd)
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        except OSError:
            raise MaintenanceError() from None

    def initialize_hold(self, operation_id: str) -> Control:
        """Explicit operator bootstrap only; refuses any existing control or bad lock."""
        _require(self._writable)
        self._check_lock()
        control = Control(1, "hold", operation_id, datetime.now(timezone.utc).isoformat())
        self._write("control", "control.json", control, create_only=True)
        return control

    def transition(self, expected_operation_id: str, expected_state: str,
                   next_control: Control) -> Control:
        """CAS under caller-owned global mutation lock; never use admission EX here."""
        _require(self._writable and type(next_control) is Control)
        _canonical_uuid(expected_operation_id)
        _require(type(expected_state) is str and expected_state in ("open", "hold"))
        current = self.read_control()
        _require(current.operation_id == expected_operation_id and current.state == expected_state)
        if next_control == current:
            return current
        _require((current.state == "hold" and next_control.state == "open"
                  and current.operation_id == next_control.operation_id)
                 or (current.state == "open" and next_control.state == "hold"
                     and current.operation_id != next_control.operation_id))
        self._write("control", "control.json", next_control)
        return next_control

    @contextmanager
    def _lease(self, exclusive: bool) -> Iterator[int]:
        with self._directory("control") as directory:
            with self._file(directory, "admission.lock", "control") as fd:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    named = os.stat("admission.lock", dir_fd=directory, follow_symlinks=False)
                    self._validate_file(named, "control")
                    _require(_inode(named) == self._lock_id == _inode(os.fstat(fd)))
                    with self._directory("control"):
                        pass
                except OSError as error:
                    if error.errno in (errno.EAGAIN, errno.EACCES):
                        raise AdmissionClosed() from None
                    raise MaintenanceError() from None
                # Closing this owned, non-inheritable description releases only its lock.
                yield fd

    def shared_lease(self):
        return self._lease(False)

    def exclusive_lease(self):
        return self._lease(True)

    def write_ack(self, acknowledgement: Ack) -> None:
        _require(type(acknowledgement) is Ack)
        self._check_lock()
        self._write("ack", "ack.json", acknowledgement)

    def read_ack(self, *, expected_operation_id: str,
                 expected_identity: ProcessIdentity) -> Ack:
        """Identity/nonce validation only; guard must ALSO require hold and EX lease."""
        _canonical_uuid(expected_operation_id)
        _require(type(expected_identity) is ProcessIdentity)
        self._check_lock()
        ack = self._read("ack", "ack.json", Ack)
        _require(ack.operation_id == expected_operation_id and
                 ProcessIdentity(ack.boot_id, ack.pid, ack.start_ticks, ack.instance_id) == expected_identity)
        return ack
