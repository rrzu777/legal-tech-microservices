"""Real protocol files and independent flock descriptions; no app dependencies."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest


SERVICE = Path(__file__).resolve().parents[2] / "estrado-pjud-service"
OPERATION = "64a8eb10-2d55-457f-924c-23d5a532c847"
NEXT_OPERATION = "71ae117a-610b-46da-9766-3841100f8710"
CREATED = "2026-08-30T12:00:00+00:00"


def load_store():
    source = SERVICE / "worker" / "maintenance_store.py"
    assert source.is_file(), "Missing secure maintenance store implementation"
    if str(SERVICE) not in sys.path:
        sys.path.insert(0, str(SERVICE))
    return importlib.import_module("worker.maintenance_store")


def test_protocol_requires_explicit_hold_bootstrap_before_read(tmp_path):
    m = load_store()
    control, ack = tmp_path.resolve() / "control", tmp_path.resolve() / "ack"
    control.mkdir(mode=0o750)
    ack.mkdir(mode=0o700)
    (control / "admission.lock").touch(mode=0o640)
    store = m.MaintenanceStore(control, ack,
                              m.StorePolicy(os.getuid(), os.getgid(), os.getuid(), os.getgid()),
                              allow_control_writes=True)
    with pytest.raises(m.MaintenanceError):
        store.read_control()
    store.initialize_hold(OPERATION)
    assert store.read_control().state == "hold"


@pytest.fixture
def protocol(tmp_path):
    module = load_store()
    root = tmp_path.resolve()
    control, ack = root / "control", root / "ack"
    control.mkdir(mode=0o750)
    ack.mkdir(mode=0o700)
    lock = control / "admission.lock"
    lock.touch(mode=0o640)
    policy = module.StorePolicy(os.getuid(), os.getgid(), os.getuid(), os.getgid())
    store = module.MaintenanceStore(control, ack, policy, allow_control_writes=True)
    return module, store, control, ack


def raw_control(control, payload):
    path = control / "control.json"
    path.write_bytes(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    path.chmod(0o640)


def control_payload(**changes):
    return dict(version=1, state="hold", operation_id=OPERATION, created_at=CREATED, **changes)


def identity(module):
    return module.ProcessIdentity("f784c8bd-67c3-448e-ae1c-55ac6feab947", 512, 9012,
                                  "bf763d76-b99c-464d-80d8-bcbd9520b923")


def acknowledgement(module, **changes):
    values = dict(version=1, operation_id=OPERATION, boot_id=identity(module).boot_id,
                  pid=512, start_ticks=9012, instance_id=identity(module).instance_id,
                  state="quiescent", inflight=0)
    values.update(changes)
    return module.Ack(**values)


def test_bootstrap_hold_cas_and_no_automatic_reopening(protocol):
    m, store, control, ack = protocol
    with pytest.raises(m.MaintenanceError):
        store.read_control()
    held = store.initialize_hold(OPERATION)
    assert held.state == "hold"
    assert held.operation_id == OPERATION
    fresh = m.MaintenanceStore(control, ack, store.policy)
    assert fresh.read_control() == held
    with pytest.raises(m.MaintenanceError):
        fresh.transition(OPERATION, "hold", m.Control(1, "open", OPERATION, CREATED))
    with pytest.raises(m.MaintenanceError):
        store.initialize_hold(NEXT_OPERATION)
    with pytest.raises(m.MaintenanceError):
        store.transition(NEXT_OPERATION, "hold", m.Control(1, "open", OPERATION, CREATED))
    assert store.read_control().state == "hold"
    opened = m.Control(1, "open", OPERATION, CREATED)
    store.transition(OPERATION, "hold", opened)
    assert store.read_control() == opened
    store.transition(OPERATION, "open", m.Control(1, "hold", NEXT_OPERATION, CREATED))
    assert store.read_control().operation_id == NEXT_OPERATION


@pytest.mark.parametrize("payload", [
    b"", b"{private-sentinel", b"x" * 8193, b"[]", b"null", b"\xff",
    b'{"version":1,"version":1,"state":"hold","operation_id":"' + OPERATION.encode()
    + b'","created_at":"2026-08-30T12:00:00Z"}',
], ids=["empty", "malformed", "oversized", "array", "null", "invalid-utf8", "duplicate-key"])
def test_bad_json_is_bounded_sanitized_and_closed(protocol, payload, capsys):
    m, store, control, _ = protocol
    raw_control(control, payload)
    with pytest.raises(m.MaintenanceError) as error:
        store.read_control()
    assert "private-sentinel" not in str(error.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("state", "OPEN"), ("operation_id", OPERATION.upper()),
    ("operation_id", "../private-sentinel"), ("created_at", "2026-08-30T12:00:00"),
    ("created_at", "2026-08-30T12:00:00-03:00"), ("created_at", "2026-02-30T00:00:00Z"),
    ("extra", "private-sentinel"),
])
def test_control_schema_rejects_unknown_fields_and_noncanonical_types(protocol, field, value):
    m, store, control, _ = protocol
    payload = control_payload()
    payload[field] = value
    raw_control(control, payload)
    with pytest.raises(m.MaintenanceError):
        store.read_control()


@pytest.mark.parametrize("mutation", ["mode", "symlink", "hardlink", "directory", "fifo"])
def test_control_metadata_rejects_unsafe_files(protocol, mutation):
    m, store, control, _ = protocol
    store.initialize_hold(OPERATION)
    path = control / "control.json"
    if mutation == "mode":
        path.chmod(0o666)
    elif mutation == "hardlink":
        os.link(path, control / "another")
    else:
        path.unlink()
        if mutation == "symlink":
            path.symlink_to(control / "admission.lock")
        elif mutation == "directory":
            path.mkdir()
        else:
            os.mkfifo(path, 0o640)
    with pytest.raises(m.MaintenanceError):
        store.read_control()
    with pytest.raises(m.MaintenanceError):
        store.transition(OPERATION, "hold", m.Control(1, "open", OPERATION, CREATED))


def test_owner_group_and_directory_policy_are_not_optional(protocol):
    m, store, control, ack = protocol
    for policy in [m.StorePolicy(os.getuid() + 1, os.getgid(), os.getuid(), os.getgid()),
                   m.StorePolicy(os.getuid(), os.getgid() + 1, os.getuid(), os.getgid()),
                   m.StorePolicy(os.getuid(), os.getgid(), os.getuid() + 1, os.getgid())]:
        with pytest.raises(m.MaintenanceError):
            m.MaintenanceStore(control, ack, policy)
    control.chmod(0o770)
    with pytest.raises(m.MaintenanceError):
        store.initialize_hold(OPERATION)


def test_symlink_parent_and_replaced_directory_are_rejected(protocol):
    m, store, control, ack = protocol
    alias = control.parent / "alias"
    alias.symlink_to(control.parent, target_is_directory=True)
    with pytest.raises(m.MaintenanceError):
        m.MaintenanceStore(alias / "control", ack, store.policy)
    control.rename(control.parent / "old-control")
    control.mkdir(mode=0o750)
    (control / "admission.lock").touch(mode=0o640)
    with pytest.raises(m.MaintenanceError):
        store.initialize_hold(OPERATION)


def test_stable_lock_must_not_be_replaced_even_with_valid_metadata(protocol):
    m, store, control, _ = protocol
    with store.shared_lease():
        (control / "admission.lock").rename(control / "old-lock")
        (control / "admission.lock").touch(mode=0o640)
        with pytest.raises(m.MaintenanceError):
            with store.exclusive_lease():
                pass
        with pytest.raises(m.MaintenanceError):
            store.read_control()


def test_missing_lock_is_not_bootstrapped(protocol):
    m, store, control, _ = protocol
    (control / "admission.lock").unlink()
    with pytest.raises(m.MaintenanceError):
        store.initialize_hold(OPERATION)
    assert not (control / "admission.lock").exists()
    assert not (control / "control.json").exists()


def test_each_operation_owns_independent_noninheritable_lock_description(protocol):
    m, store, _, _ = protocol
    with store.shared_lease() as first:
        assert not os.get_inheritable(first)
        with store.shared_lease() as second:
            assert first != second
            assert not os.get_inheritable(second)
            with pytest.raises(m.AdmissionClosed):
                with store.exclusive_lease():
                    pass
        with pytest.raises(m.AdmissionClosed):
            with store.exclusive_lease():
                pass
    with store.exclusive_lease():
        with pytest.raises(m.AdmissionClosed):
            with store.shared_lease():
                pass


def test_real_second_process_cannot_acquire_exclusive_until_every_lease_finishes(protocol):
    _, store, control, _ = protocol
    code = """import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(23)
finally:
    os.close(fd)
"""
    def probe():
        return subprocess.run([sys.executable, "-c", code, str(control / "admission.lock")],
                              capture_output=True, timeout=5).returncode
    with store.shared_lease():
        with store.shared_lease():
            assert probe() == 23
        assert probe() == 23
    assert probe() == 0


def test_atomic_control_write_has_exact_metadata_and_no_temporary_leaks(protocol):
    m, store, control, _ = protocol
    store.initialize_hold(OPERATION)
    old_inode = (control / "control.json").stat().st_ino
    store.transition(OPERATION, "hold", m.Control(1, "open", OPERATION, CREATED))
    assert (control / "control.json").stat().st_ino != old_inode
    assert (control / "control.json").stat().st_mode & 0o7777 == 0o640
    assert sorted(p.name for p in control.iterdir()) == ["admission.lock", "control.json"]


def test_ack_roundtrip_validates_exact_identity_and_nonce(protocol):
    m, store, _, ack_dir = protocol
    store.initialize_hold(OPERATION)
    ack = acknowledgement(m)
    store.write_ack(ack)
    assert store.read_ack(expected_operation_id=OPERATION, expected_identity=identity(m)) == ack
    assert (ack_dir / "ack.json").stat().st_mode & 0o7777 == 0o600
    with pytest.raises(m.MaintenanceError):
        store.read_ack(expected_operation_id=NEXT_OPERATION, expected_identity=identity(m))
    for field, value in [("boot_id", str(uuid.uuid4())), ("pid", 513), ("start_ticks", 9013),
                         ("instance_id", str(uuid.uuid4()))]:
        values = vars(identity(m)).copy()
        values[field] = value
        with pytest.raises(m.MaintenanceError):
            store.read_ack(expected_operation_id=OPERATION, expected_identity=m.ProcessIdentity(**values))


@pytest.mark.parametrize("field,value", [("inflight", True), ("inflight", -1), ("inflight", 1),
    ("pid", True), ("pid", 0), ("start_ticks", False), ("start_ticks", 0),
    ("version", True), ("state", "uncertain"), ("extra", "private-sentinel")])
def test_ack_schema_rejects_false_quiescence_and_bad_types(protocol, field, value):
    m, store, _, ack_dir = protocol
    values = vars(acknowledgement(m)).copy()
    values[field] = value
    (ack_dir / "ack.json").write_text(json.dumps(values))
    (ack_dir / "ack.json").chmod(0o600)
    with pytest.raises(m.MaintenanceError):
        store.read_ack(expected_operation_id=OPERATION, expected_identity=identity(m))


def test_ack_write_rejects_existing_wrong_mode_or_link(protocol):
    m, store, _, ack_dir = protocol
    store.write_ack(acknowledgement(m))
    (ack_dir / "ack.json").chmod(0o644)
    with pytest.raises(m.MaintenanceError):
        store.write_ack(acknowledgement(m))
    (ack_dir / "ack.json").unlink()
    (ack_dir / "ack.json").symlink_to(ack_dir / "private-sentinel")
    with pytest.raises(m.MaintenanceError):
        store.write_ack(acknowledgement(m))


@pytest.mark.skipif(sys.platform != "linux", reason="/proc identity is a Linux production contract")
def test_current_process_identity_matches_kernel():
    m = load_store()
    got = m.ProcessIdentity.current()
    assert got.pid == os.getpid()
    assert got.boot_id == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert got.start_ticks == int(Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19])
    assert got.instance_id != m.ProcessIdentity.current().instance_id


@pytest.mark.parametrize("payload", [b"", b"x" * 8193, b'{"version":1,"version":1}',
                                     b'{"inflight":NaN}', b'{}'])
def test_ack_invalid_json_never_supplies_identity_proof(protocol, payload):
    m, store, _, ack_dir = protocol
    (ack_dir / "ack.json").write_bytes(payload)
    (ack_dir / "ack.json").chmod(0o600)
    with pytest.raises(m.MaintenanceError):
        store.read_ack(expected_operation_id=OPERATION, expected_identity=identity(m))


@pytest.mark.skipif(os.geteuid() != 0, reason="real owner mutation requires Linux/root fixture")
@pytest.mark.parametrize("name,kind", [("control.json", "control"), ("admission.lock", "control"),
                                     ("ack.json", "ack")])
def test_wrong_file_owner_is_rejected_even_when_parent_is_valid(protocol, name, kind):
    m, store, control, ack_dir = protocol
    store.initialize_hold(OPERATION)
    store.write_ack(acknowledgement(m))
    path = (control if kind == "control" else ack_dir) / name
    os.chown(path, 12345, os.getgid())
    with pytest.raises(m.MaintenanceError):
        if kind == "control":
            store.read_control()
        else:
            store.read_ack(expected_operation_id=OPERATION, expected_identity=identity(m))


def test_fsync_failure_before_rename_preserves_hold_and_removes_temporary(protocol, monkeypatch):
    m, store, control, _ = protocol
    store.initialize_hold(OPERATION)
    existing = (control / "control.json").read_bytes()
    def broken_fsync(fd):
        raise OSError("private-sentinel")
    monkeypatch.setattr(m.os, "fsync", broken_fsync)
    with pytest.raises(m.MaintenanceError) as error:
        store.transition(OPERATION, "hold", m.Control(1, "open", OPERATION, CREATED))
    assert "private-sentinel" not in str(error.value)
    assert (control / "control.json").read_bytes() == existing
    assert sorted(p.name for p in control.iterdir()) == ["admission.lock", "control.json"]


def test_production_uses_named_estrado_group_and_root_only_operator(protocol, monkeypatch):
    import grp
    m = protocol[0]
    # OS identity lookup is the boundary here, never the protocol filesystem.
    monkeypatch.setattr(m.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1234, pw_gid=7777))
    monkeypatch.setattr(grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=4321))
    monkeypatch.setattr(m.os, "geteuid", lambda: 1234)
    class Capture(m.MaintenanceStore):
        def __init__(self, control_dir, ack_dir, policy, *, allow_control_writes=False):
            self.paths = (control_dir, ack_dir)
            self.policy = policy
            self.writable = allow_control_writes
    worker = Capture.production()
    assert worker.paths == ("/var/lib/worker-maintenance", "/run/worker-maintenance")
    assert worker.policy.control_uid == 0
    assert worker.policy.control_gid == 4321
    assert worker.policy.ack_uid == 1234
    assert worker.policy.ack_gid == 4321
    assert not worker.writable
    with pytest.raises(m.MaintenanceError):
        Capture.production(operator=True)
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    assert Capture.production(operator=True).writable
