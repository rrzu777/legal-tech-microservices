"""Initial bootstrap: real metadata/store/locks; external systemd/kernel boundaries."""
from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/bootstrap-worker-maintenance.py"
SHA = "a" * 40
BOOT = "11111111-2222-4333-8444-555555555555"
NOW = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def module():
    assert SCRIPT.is_file(), "stopped-only bootstrap implementation missing"
    spec = importlib.util.spec_from_file_location("worker_bootstrap", SCRIPT)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def test_legacy_target_preserves_all_bytes_except_two_runtime_lines(module):
    legacy = (ROOT / "ops/systemd/estrado-pjud-worker.service").read_text().replace(
        "RuntimeDirectory=worker-maintenance\nRuntimeDirectoryMode=0700\n", "")
    target = module.target_unit(legacy)
    assert target.replace("RuntimeDirectory=worker-maintenance\nRuntimeDirectoryMode=0700\n", "") == legacy
    assert target.count("RuntimeDirectory=worker-maintenance\n") == 1


@pytest.mark.parametrize("suffix", [
    "\n[Service]\nRuntimeDirectory=other\n", "\n[Service]\nRuntimeDirectoryMode=0777\n",
    "\n[Service]\nUser=root\n", "\n[Service]\nExecStartPre=/bin/true\n",
])
def test_unsafe_or_already_initialized_unit_rejected(module, suffix):
    legacy = (ROOT / "ops/systemd/estrado-pjud-worker.service").read_text().replace(
        "RuntimeDirectory=worker-maintenance\nRuntimeDirectoryMode=0700\n", "")
    with pytest.raises(module.MaintenanceError):
        module.target_unit(legacy + suffix)


def test_production_cli_has_no_path_or_test_mode_override(module):
    for extra in (["--test-mode"], ["--repo-dir", "/tmp/unsafe"], ["--proof-file", "/tmp/proof"]):
        with pytest.raises(SystemExit) as error:
            module.parser().parse_args(["install", "--expected-sha", SHA, *extra])
        assert error.value.code == 2


def test_daytime_override_is_explicit_and_boolean(module):
    args = module.parser().parse_args([
        "install", "--expected-sha", SHA, "--allow-daytime-maintenance",
    ])
    assert args.allow_daytime_maintenance is True
    assert module.parser().parse_args([
        "install", "--expected-sha", SHA,
    ]).allow_daytime_maintenance is False


@pytest.fixture
def host(module, tmp_path):
    root = tmp_path.resolve()
    repo = root / "repo"
    repo.mkdir()
    git_env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    # Keep metadata real so independent probes can add synthetic commits to this fixture.
    for args in (("init", "--quiet"), ("read-tree", "--empty")):
        subprocess.run(["/usr/bin/git", "-C", str(repo), *args], env=git_env,
                       check=True, capture_output=True, timeout=10)
    systemd = root / "systemd"
    systemd.mkdir()
    for unit in ("estrado-pjud.service", "estrado-pjud-worker.service"):
        contents = (ROOT / "ops/systemd" / unit).read_text().replace(
            "RuntimeDirectory=worker-maintenance\nRuntimeDirectoryMode=0700\n", "")
        (systemd / unit).write_text(contents)
        (systemd / unit).chmod(0o644)
    dropdir = systemd / "estrado-pjud-worker.service.d"
    dropdir.mkdir()
    drop = dropdir / "xvfb.conf"
    drop.write_text((ROOT / "ops/systemd/estrado-pjud-worker.service.d/xvfb.conf").read_text())
    drop.chmod(0o644)
    proc = root / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text(BOOT)
    cgroup = root / "cgroup"
    cgroup.mkdir()
    for unit in ("estrado-pjud.service", "estrado-pjud-worker.service"):
        group = cgroup / "system.slice" / unit
        group.mkdir(parents=True)
        (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
        (group / "cgroup.procs").write_text("")
    global_lock = root / "global.lock"
    global_lock.touch(mode=0o600)
    for path in root.rglob("*"):
        os.chown(path, os.getuid(), os.getgid())
    config = module.Config(SHA, repo_dir=repo, systemd_dir=systemd, proc_root=proc,
                           cgroup_root=cgroup, control_dir=root / "control", ack_dir=root / "ack",
                           journal_root=root / "journals", bootstrap_root=root / "bootstrap",
                           global_lock=global_lock, root_uid=os.getuid(), root_gid=os.getgid(),
                           worker_uid=os.getuid(), worker_gid=os.getgid(), clock=lambda: NOW)
    services = {}
    for number, unit in enumerate(("estrado-pjud.service", "estrado-pjud-worker.service"), 21):
        services[unit] = dict(LoadState="loaded", FragmentPath=str(systemd / unit),
            DropInPaths=str(drop) if "worker" in unit else "", NeedDaemonReload="no",
            UnitFileState="disabled", ActiveState="inactive", SubState="dead", Result="success",
            MainPID="0", ExecMainPID=str(number), ExecMainCode="1", ExecMainStatus="0",
            ExecMainExitTimestampMonotonic="1234567", ControlGroup="", Slice="system.slice", Job="")
    state = SimpleNamespace(sha=SHA, tree="", git_keys="core.repositoryformatversion\0", modes="100644\n", tags="H ops/sample.py\0",
                            services=services, before_command=None, healthy=True)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if state.before_command:
            state.before_command(command)
        assert kwargs["timeout"] <= 10
        if command[0] == "/usr/bin/git":
            if "config" in command:
                output = state.git_keys
            elif "ls-files" in command:
                output = state.tags if "-v" in command else state.modes
            elif "rev-parse" in command:
                if "--show-toplevel" in command:
                    output = str(repo) + "\n"
                elif "--absolute-git-dir" in command or "--git-common-dir" in command:
                    output = str(repo / ".git") + "\n"
                else:
                    output = state.sha + "\n"
            elif "status" in command:
                output = state.tree
            else:
                raise AssertionError(command)
        elif command[:2] == ["/usr/bin/systemctl", "show"]:
            properties = [item.split("=", 1)[1] for item in command[3:]]
            output = "".join(f"{key}={state.services[command[2]][key]}\n" for key in properties)
        elif command[:2] == ["/usr/bin/systemctl", "is-active"]:
            return subprocess.CompletedProcess(command, 0 if state.healthy else 1, b"", b"")
        else:
            raise AssertionError("lifecycle or unexpected command: " + str(command))
        return subprocess.CompletedProcess(command, 0, output.encode(), b"")

    return SimpleNamespace(module=module, config=config, root=root, state=state, calls=calls,
                           runner=runner, drop=drop, unit=systemd / "estrado-pjud-worker.service")


def install(host):
    return host.module.install(host.config, host.runner)


def test_install_stays_closed_and_preserves_dropin_without_lifecycle(host):
    original, drop = host.unit.read_bytes(), host.drop.read_bytes()
    result = install(host)
    assert result["phase"] == "installed" and result["result"] == "succeeded"
    store = host.module.store_for(host.config)
    assert store.read_control().state == "hold"
    assert store.read_control().operation_id == result["operation_id"]
    assert host.drop.read_bytes() == drop
    assert (host.config.bootstrap_root / "worker-unit.original").read_bytes() == original
    assert not host.config.ack_dir.exists()
    assert not host.config.journal_root.exists()
    host.module.operator.validate_unit(host.module.operator_args(host.config))


@pytest.mark.parametrize("service", ["estrado-pjud.service", "estrado-pjud-worker.service"])
@pytest.mark.parametrize("key,value", [
    ("ActiveState", "active"), ("SubState", "failed"), ("Result", "signal"),
    ("MainPID", "77"), ("ExecMainPID", "0"), ("ExecMainCode", "2"), ("ExecMainStatus", "15"),
    ("ExecMainExitTimestampMonotonic", "0"), ("UnitFileState", "enabled"),
    ("UnitFileState", "masked"), ("UnitFileState", "static"), ("Job", "22 start"),
    ("NeedDaemonReload", "yes"), ("DropInPaths", "/tmp/unowned.conf"),
])
def test_unclean_or_enabled_services_never_create_control(host, service, key, value):
    host.state.services[service][key] = value
    with pytest.raises((host.module.MaintenanceError, host.module.audit.Unavailable, OSError, ValueError)):
        install(host)
    assert not host.config.control_dir.exists()
    assert not host.config.bootstrap_root.exists()


@pytest.mark.parametrize("fault", ["sha", "tree", "filter", "submodule", "window", "control", "ack", "journal", "record",
                                   "unit-mode", "unit-link", "unit-hardlink", "group", "proc", "old-pid", "lock-mode", "lock-missing", "busy"])
def test_unsafe_prerequisites_fail_before_bootstrap_write(host, fault):
    if fault == "sha": host.state.sha = "b" * 40
    elif fault == "tree": host.state.tree = " M ops/worker-maintenance.py\n"
    elif fault == "filter": host.state.git_keys += "filter.evil.clean\0"
    elif fault == "submodule": host.state.modes = "160000\n"
    elif fault == "window": host.config.clock = lambda: NOW.replace(hour=16)
    elif fault in {"control", "ack", "journal", "record"}:
        {"control": host.config.control_dir, "ack": host.config.ack_dir,
         "journal": host.config.journal_root, "record": host.config.bootstrap_root}[fault].mkdir()
    elif fault == "unit-mode": host.unit.chmod(0o666)
    elif fault == "unit-link":
        host.unit.rename(host.unit.with_suffix(".saved"))
        host.unit.symlink_to(host.unit.with_suffix(".saved"))
    elif fault == "unit-hardlink": os.link(host.unit, host.root / "unit-copy")
    elif fault == "group":
        (host.config.cgroup_root / "system.slice/estrado-pjud-worker.service/cgroup.procs").write_text("99\n")
    elif fault in {"proc", "old-pid"}:
        process = host.config.proc_root / ("21" if fault == "old-pid" else "99")
        process.mkdir()
        (process / "cgroup").write_text("0::/system.slice/estrado-pjud-worker.service/child\n")
    elif fault == "lock-mode": host.config.global_lock.chmod(0o644)
    elif fault == "lock-missing": host.config.global_lock.unlink()
    before = host.unit.read_bytes()
    with open(host.root / "busy.lock", "w") if fault != "busy" else open(host.config.global_lock) as lock:
        if fault == "busy": fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((host.module.MaintenanceError, host.module.audit.Unavailable, OSError, ValueError)): install(host)
    assert not (host.config.control_dir / "control.json").exists()
    assert host.unit.read_bytes() == before


@pytest.mark.parametrize("service,metadata", [
    ("estrado-pjud.service", {"ExecMainCode": "2", "ExecMainStatus": "15"}),
    ("estrado-pjud-worker.service", {
        "ExecMainPID": "0", "ExecMainCode": "0", "ExecMainStatus": "0",
        "ExecMainExitTimestampMonotonic": "0",
    }),
])
def test_stopped_snapshot_accepts_systemd_termination_without_claiming_drain(host, service, metadata):
    host.state.services[service].update(metadata)
    boot, services = host.module.stopped_snapshot(host.config, host.runner)
    assert boot == BOOT
    assert services[service] == host.state.services[service]
    # This is a read-only kernel predicate; business closure is a separate prerequisite.
    assert not host.config.control_dir.exists()


@pytest.mark.parametrize("forgotten", [False, True])
@pytest.mark.parametrize("fault", ["active", "failed", "job", "cgroup"])
def test_alternative_termination_metadata_does_not_allow_execution(host, forgotten, fault):
    values = host.state.services["estrado-pjud-worker.service"]
    values.update(ExecMainCode="2", ExecMainStatus="15")
    if forgotten:
        values.update(ExecMainCode="0", ExecMainStatus="0", ExecMainPID="0",
                      ExecMainExitTimestampMonotonic="0")
    if fault == "active": values["ActiveState"] = "active"
    elif fault == "failed": values["Result"] = "watchdog"
    elif fault == "job": values["Job"] = "42 start"
    else:
        (host.config.cgroup_root / "system.slice/estrado-pjud-worker.service/cgroup.procs").write_text("99\n")
    with pytest.raises(host.module.MaintenanceError):
        host.module.stopped_snapshot(host.config, host.runner)
    assert not host.config.control_dir.exists()


def test_changed_runtime_between_reads_rejects_stale_stopped_snapshot(host):
    reads = 0
    def change(command):
        nonlocal reads
        if command[:3] == ["/usr/bin/systemctl", "show", "estrado-pjud-worker.service"]:
            reads += 1
            if reads == 2:
                host.state.services[command[2]]["ExecMainExitTimestampMonotonic"] = "1234999"
    host.state.before_command = change
    with pytest.raises((host.module.MaintenanceError, host.module.audit.Unavailable, OSError, ValueError)): install(host)
    assert not host.config.control_dir.exists()
    assert not host.config.bootstrap_root.exists()


def test_explicit_daytime_override_allows_only_otherwise_safe_install(host):
    host.config.clock = lambda: NOW.replace(hour=16)
    assert host.module.execute(host.config, "install", None, host.runner)["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()

    host.config.allow_daytime_maintenance = True
    assert host.module.execute(host.config, "install", None, host.runner)["result"] == "succeeded"


def test_daytime_override_does_not_bypass_exact_tree_or_stopped_services(host):
    host.config.clock = lambda: NOW.replace(hour=16)
    host.config.allow_daytime_maintenance = True
    host.state.tree = " M ops/bootstrap-worker-maintenance.py\n"
    assert host.module.execute(host.config, "install", None, host.runner)["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()

    host.state.tree = ""
    host.state.services["estrado-pjud-worker.service"]["ActiveState"] = "active"
    assert host.module.execute(host.config, "install", None, host.runner)["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()


@pytest.fixture
def closed_worker(host, monkeypatch):
    from worker.maintenance_store import Ack, ProcessIdentity
    result = install(host)
    operation = result["operation_id"]
    ackdir = host.config.ack_dir
    ackdir.mkdir(mode=0o700)
    os.chown(ackdir, os.getuid(), os.getgid())
    identity = ProcessIdentity(BOOT, 512, 9012, "bf763d76-b99c-464d-80d8-bcbd9520b923")
    store = host.module.store_for(host.config)
    store.write_ack(Ack(1, operation, identity.boot_id, identity.pid, identity.start_ticks,
                        identity.instance_id, "quiescent", 0))
    process = host.config.proc_root / "512"
    process.mkdir()
    (process / "stat").write_text("512 (worker) " + " ".join(["S"] + ["0"] * 18 + ["9012"]))
    (process / "cgroup").write_text("0::/system.slice/estrado-pjud-worker.service\n")
    runtime = host.state.services["estrado-pjud-worker.service"]
    runtime.update(ActiveState="active", SubState="running", MainPID="512",
                   ControlGroup="/system.slice/estrado-pjud-worker.service")
    host.state.services["estrado-pjud.service"].update(ActiveState="active", SubState="running", MainPID="513")
    monkeypatch.setattr(host.module.operator.subprocess, "run", host.runner)

    def kernel(args, pid, instance):
        boot = (host.config.proc_root / "sys/kernel/random/boot_id").read_text().strip()
        text = (host.config.proc_root / str(pid) / "stat").read_text()
        return ProcessIdentity(boot, pid, int(text.rsplit(")", 1)[1].split()[19]), instance)
    monkeypatch.setattr(host.module.operator, "kernel_identity", kernel)

    class HealthResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
    monkeypatch.setattr(host.module.operator.urllib.request, "urlopen", lambda *a, **kw: HealthResponse())
    host.operation, host.identity, host.store = operation, identity, store
    return host


def test_adoption_authenticates_first_identity_and_creates_normal_journal_without_release(closed_worker):
    host = closed_worker
    result = host.module.adopt(host.config, host.operation, host.runner)
    assert result == dict(operation_id=host.operation, phase="adopted", result="succeeded")
    assert host.store.read_control().state == "hold"
    args = host.module.operator_args(host.config)
    journal = host.module.operator.journal_read(args, host.operation)
    assert journal == dict(version=1, operation_id=host.operation,
        initial_identity=f"{BOOT}:512:9012:bf763d76-b99c-464d-80d8-bcbd9520b923",
        drained_identity=f"{BOOT}:512:9012:bf763d76-b99c-464d-80d8-bcbd9520b923", result="intended")


def verify_adopted(host, monkeypatch):
    def validate_held_fd(fd, path, uid, gid, mode):
        named = os.lstat(path)
        opened = os.fstat(fd)
        host.module.operator.metadata(named, uid, gid, mode)
        host.module.operator.metadata(opened, uid, gid, mode)
        assert (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino)
    monkeypatch.setattr(host.module.operator, "validate_held_fd", validate_held_fd)
    global_fd = os.open(host.config.global_lock, os.O_RDWR | os.O_NOFOLLOW)
    admission_fd = os.open(host.config.control_dir / "admission.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(global_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(admission_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return host.module.verify_adopted(
            host.config, host.operation, host.module.operator.identity_text(host.identity),
            global_fd, admission_fd, host.runner,
        )
    finally:
        os.close(admission_fd)
        os.close(global_fd)


def test_adopted_bootstrap_can_be_verified_under_controller_owned_leases(closed_worker, monkeypatch):
    host = closed_worker
    host.module.adopt(host.config, host.operation, host.runner)
    assert verify_adopted(host, monkeypatch) == {
        "operation_id": host.operation, "phase": "adopted", "result": "verified",
    }
    assert host.store.read_control().state == "hold"


@pytest.mark.parametrize("fault", ["record-phase", "journal-result", "wrong-identity"])
def test_adopted_verifier_rejects_drift_without_opening(closed_worker, fault, monkeypatch):
    host = closed_worker
    host.module.adopt(host.config, host.operation, host.runner)
    if fault == "record-phase":
        path = host.config.bootstrap_root / "record.json"
        value = json.loads(path.read_text())
        value["phase"] = "installed"
        path.write_text(json.dumps(value) + "\n")
    elif fault == "journal-result":
        path = host.config.journal_root / f"{host.operation}.json"
        value = json.loads(path.read_text())
        value["result"] = "succeeded"
        path.write_text(json.dumps(value) + "\n")
    else:
        host.identity = host.identity.__class__(
            host.identity.boot_id, host.identity.pid, host.identity.start_ticks + 1, host.identity.instance_id,
        )
    with pytest.raises((host.module.MaintenanceError, host.module.audit.Unavailable, OSError, ValueError)):
        verify_adopted(host, monkeypatch)
    assert host.store.read_control().state == "hold"


@pytest.mark.parametrize("fault", ["wrong-operation", "wrong-sha", "record-phase", "record-mode", "record-hash",
    "target-drift", "drop-drift", "journal", "draining", "old-boot", "pid-reuse", "wrong-cgroup", "wrong-mainpid", "busy", "health", "open"])
def test_adoption_rejects_untrusted_identity_or_state_without_journal_or_release(closed_worker, fault):
    host = closed_worker
    from worker.maintenance_store import Ack, Control
    operation = host.operation
    if fault == "wrong-operation": operation = "88888888-2222-4333-8444-555555555555"
    elif fault == "wrong-sha": host.state.sha = "b" * 40
    elif fault.startswith("record-"):
        path = host.config.bootstrap_root / "record.json"
        record = json.loads(path.read_text())
        if fault == "record-mode": path.chmod(0o644)
        else:
            record["phase" if fault == "record-phase" else "target_hash"] = "prepared" if fault == "record-phase" else "c" * 64
            path.write_text(json.dumps(record))
    elif fault == "target-drift": host.unit.write_text(host.unit.read_text() + "\n# changed\n")
    elif fault == "drop-drift": host.drop.write_text(host.drop.read_text() + "\n# changed\n")
    elif fault == "journal": host.config.journal_root.mkdir()
    elif fault == "draining":
        i = host.identity
        host.store.write_ack(Ack(1, operation, i.boot_id, i.pid, i.start_ticks, i.instance_id, "draining", 1))
    elif fault == "old-boot": (host.config.proc_root / "sys/kernel/random/boot_id").write_text("88888888-2222-4333-8444-555555555555")
    elif fault == "pid-reuse":
        path = host.config.proc_root / "512/stat"
        path.write_text(path.read_text().replace("9012", "9999"))
    elif fault == "wrong-cgroup": (host.config.proc_root / "512/cgroup").write_text("0::/other.slice\n")
    elif fault == "wrong-mainpid": host.state.services["estrado-pjud-worker.service"]["MainPID"] = "999"
    elif fault == "health": host.state.healthy = False
    elif fault == "open": host.store.transition(operation, "hold", Control(1, "open", operation, NOW.isoformat()))
    with host.store.shared_lease() if fault == "busy" else open(host.config.global_lock) as _:
        with pytest.raises((host.module.MaintenanceError, host.module.audit.Unavailable, OSError, ValueError)):
            host.module.adopt(host.config, operation, host.runner)
    assert not (host.config.journal_root / f"{host.operation}.json").exists()
    assert host.store.read_control().state == ("open" if fault == "open" else "hold")


def test_runtime_change_during_health_rejects_adoption_before_journal(closed_worker):
    host = closed_worker
    def change(command):
        if command[:2] == ["/usr/bin/systemctl", "is-active"]:
            host.state.services["estrado-pjud-worker.service"]["MainPID"] = "999"
    host.state.before_command = change
    with pytest.raises((host.module.MaintenanceError, host.module.audit.Unavailable, OSError, ValueError)):
        host.module.adopt(host.config, host.operation, host.runner)
    assert not host.config.journal_root.exists()
    assert host.store.read_control().state == "hold"


@pytest.mark.parametrize("failure_number", range(1, 19))
def test_every_install_fsync_failure_is_fail_closed_and_preserves_partial_evidence(host, monkeypatch, failure_number):
    original = os.fsync
    count = 0
    def fail(fd):
        nonlocal count
        count += 1
        if count == failure_number:
            raise OSError("private-filesystem-detail")
        return original(fd)
    with monkeypatch.context() as scope:
        scope.setattr(os, "fsync", fail)
        result = host.module.execute(host.config, "install", None, host.runner)
    assert count >= failure_number
    assert result["result"] == "blocked"
    assert "private-filesystem-detail" not in json.dumps(result)
    control = host.config.control_dir / "control.json"
    if control.exists():
        assert host.module.store_for(host.config).read_control().state == "hold"
    assert not host.config.journal_root.exists()
    assert host.config.bootstrap_root.exists()
    assert host.module.execute(host.config, "install", None, host.runner)["result"] == "blocked"


@pytest.mark.parametrize("failure_number", range(1, 6))
def test_adoption_persistence_failure_never_opens_or_retries_journal(closed_worker, monkeypatch, failure_number):
    host = closed_worker
    original = os.fsync
    count = 0
    def fail(fd):
        nonlocal count
        count += 1
        if count == failure_number: raise OSError("private-journal-detail")
        return original(fd)
    with monkeypatch.context() as scope:
        scope.setattr(os, "fsync", fail)
        result = host.module.execute(host.config, "adopt", host.operation, host.runner)
    assert result["result"] == "blocked"
    assert count >= failure_number
    assert host.store.read_control().state == "hold"
    assert host.module.execute(host.config, "adopt", host.operation, host.runner)["result"] == "blocked"


def test_cli_linux_root_gate_does_not_probe_services(module, monkeypatch, capsys):
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(module.sys, "platform", "linux")
    assert module.main(["install", "--expected-sha", SHA]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output == {"operation_id": None, "phase": "validation", "result": "blocked"}


@pytest.mark.parametrize("arguments", [["install", "--expected-sha", "bad"],
    ["adopt", "--expected-sha", SHA], ["install", "--expected-sha", SHA, "--operation-id", BOOT]])
def test_invalid_cli_contract_cannot_reach_mutation(module, monkeypatch, capsys, arguments):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.sys, "platform", "linux")
    assert module.main(arguments) == 2
    assert json.loads(capsys.readouterr().out)["result"] == "blocked"


def test_first_adoption_journal_is_accepted_by_existing_explicit_finish(closed_worker, capsys):
    host = closed_worker
    host.module.adopt(host.config, host.operation, host.runner)
    assert host.store.read_control().state == "hold"
    config = host.config
    args = ["--test-mode", "--control-dir", str(config.control_dir), "--ack-dir", str(config.ack_dir),
        "--proc-root", str(config.proc_root), "--systemctl", "/usr/bin/systemctl",
        "--global-lock", str(config.global_lock), "--journal-root", str(config.journal_root),
        "--health-url", "http://127.0.0.1:8000/api/v1/health",
        "--root-uid", str(config.root_uid), "--root-gid", str(config.root_gid),
        "--worker-uid", str(config.worker_uid), "--worker-gid", str(config.worker_gid),
        "finish", "--operation-id", host.operation, "--identity", host.module.operator.identity_text(host.identity)]
    assert host.module.operator.main(args) == 0
    assert host.store.read_control().state == "open"
    journal = host.module.operator.journal_read(host.module.operator_args(config), host.operation)
    assert journal["result"] == "succeeded"
    capsys.readouterr()


@pytest.mark.parametrize("boundary", ["backup", "unit", "control", "admission", "write-zero", "mkdir", "chown"])
def test_install_file_side_effect_failures_stay_closed_without_lifecycle(host, monkeypatch, boundary):
    original_replace, original_open = os.replace, os.open
    reached = False
    def replace(source, target, **kwargs):
        nonlocal reached
        target_name = Path(target).name
        fail_name = {"backup": "worker-unit.original", "unit": "estrado-pjud-worker.service", "control": "control.json"}.get(boundary)
        if target_name == fail_name:
            reached = True
            raise OSError("private-rename-detail")
        return original_replace(source, target, **kwargs)
    def opened(path, flags, *args, **kwargs):
        nonlocal reached
        if boundary == "admission" and Path(path).name == "admission.lock":
            reached = True
            raise OSError("private-open-detail")
        return original_open(path, flags, *args, **kwargs)
    def fail(*args, **kwargs):
        nonlocal reached
        reached = True
        if boundary == "write-zero": return 0
        raise OSError("private-metadata-detail")
    with monkeypatch.context() as scope:
        scope.setattr(os, "replace", replace)
        scope.setattr(os, "open", opened)
        if boundary == "write-zero": scope.setattr(os, "write", fail)
        if boundary == "mkdir": scope.setattr(Path, "mkdir", fail)
        if boundary == "chown": scope.setattr(os, "chown", fail)
        result = host.module.execute(host.config, "install", None, host.runner)
    assert reached
    assert result["result"] == "blocked"
    assert not (host.config.control_dir / "control.json").exists()
    assert not host.config.journal_root.exists()
    assert "private-" not in json.dumps(result)


def test_boot_change_between_observations_blocks_before_first_write(host):
    reads = 0
    def change(command):
        nonlocal reads
        if command[:3] == ["/usr/bin/systemctl", "show", "estrado-pjud-worker.service"]:
            reads += 1
            if reads == 2:
                (host.config.proc_root / "sys/kernel/random/boot_id").write_text("88888888-2222-4333-8444-555555555555")
    host.state.before_command = change
    result = host.module.execute(host.config, "install", None, host.runner)
    assert result["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()


def test_window_closing_during_preflight_blocks_before_first_write(host):
    calls = 0
    def clock():
        nonlocal calls
        calls += 1
        return NOW if calls == 1 else NOW.replace(hour=16)
    host.config.clock = clock
    assert host.module.execute(host.config, "install", None, host.runner)["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()


@pytest.mark.parametrize("tag", ["h", "S", "s"])
def test_git_flags_that_hide_tracked_changes_cannot_authorize_install(host, tag):
    host.state.tags = tag + " estrado-pjud-service/worker/__main__.py\0"
    result = host.module.execute(host.config, "install", None, host.runner)
    assert result["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()


def test_dangling_optional_dropin_is_not_treated_as_absent(host):
    host.drop.unlink()
    host.drop.symlink_to(host.root / "missing.conf")
    host.state.services["estrado-pjud-worker.service"]["DropInPaths"] = ""
    result = host.module.execute(host.config, "install", None, host.runner)
    assert result["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()


@pytest.fixture
def real_git_host(host):
    """Synthetic committed code only; real Git commands never touch this checkout."""
    repo = host.config.repo_dir
    for name in ("HEAD", "index", "config"):
        (repo / ".git" / name).unlink()
    environment = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"}
    def git(*args):
        return subprocess.run(["/usr/bin/git", "-C", str(repo), *args],
            env=environment, capture_output=True, check=True, timeout=10)
    relative = Path("estrado-pjud-service/worker/__main__.py")
    original = repo / relative
    original.parent.mkdir(parents=True)
    original.write_text("reviewed synthetic code\n")
    git("init", "--quiet")
    git("add", str(relative))
    git("-c", "user.name=Synthetic test", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "synthetic fixture")
    host.config.expected_sha = git("rev-parse", "HEAD").stdout.decode().strip()
    def runner(command, **kwargs):
        if command[0] == "/usr/bin/git":
            return subprocess.run(command, **kwargs)
        return host.runner(command, **kwargs)
    host.real_runner, host.git, host.original, host.relative = runner, git, original, relative
    return host


def test_real_git_redirected_tree_cannot_authorize_changed_deployment(real_git_host):
    host = real_git_host
    alternate = host.root / "other-tree"
    (alternate / host.relative).parent.mkdir(parents=True)
    (alternate / host.relative).write_bytes(host.original.read_bytes())
    host.git("config", "core.worktree", str(alternate))
    host.original.write_text("UNREVIEWED synthetic code\n")
    assert host.git("status", "--porcelain=v1", "--untracked-files=all").stdout == b""
    assert host.git("rev-parse", "--show-toplevel").stdout.decode().strip() == str(alternate)
    result = host.module.execute(host.config, "install", None, host.real_runner)
    assert result["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()
    assert not host.config.control_dir.exists()


def test_real_git_exact_deployment_allows_closed_install(real_git_host):
    host = real_git_host
    result = host.module.execute(host.config, "install", None, host.real_runner)
    assert result["result"] == "succeeded"
    assert host.module.store_for(host.config).read_control().state == "hold"


def test_real_git_linked_worktree_with_trusted_metadata_allows_closed_install(real_git_host):
    host = real_git_host
    deployment = host.root / "linked-deployment"
    host.git("worktree", "add", "--detach", str(deployment), host.config.expected_sha)
    host.config.repo_dir = deployment
    result = host.module.execute(host.config, "install", None, host.real_runner)
    assert result["result"] == "succeeded"
    assert host.module.store_for(host.config).read_control().state == "hold"


@pytest.mark.parametrize("fault", ["gitdir-writable", "config-writable", "index-link", "config-hardlink"])
def test_untrusted_git_metadata_cannot_authorize_install(real_git_host, fault):
    host = real_git_host
    gitdir = host.config.repo_dir / ".git"
    if fault == "gitdir-writable": gitdir.chmod(0o777)
    elif fault == "config-writable": (gitdir / "config").chmod(0o666)
    elif fault == "index-link":
        (gitdir / "index").rename(host.root / "index")
        (gitdir / "index").symlink_to(host.root / "index")
    elif fault == "config-hardlink": os.link(gitdir / "config", host.root / "git-config-copy")
    result = host.module.execute(host.config, "install", None, host.real_runner)
    assert result["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777])
def test_nonsticky_writable_global_parent_rejected_before_install(host, mode):
    parent = host.root / "unsafe-lock-parent"
    parent.mkdir()
    parent.chmod(mode)
    host.config.global_lock.rename(parent / "global.lock")
    host.config.global_lock = parent / "global.lock"
    inode = host.config.global_lock.stat().st_ino
    result = host.module.execute(host.config, "install", None, host.runner)
    assert result["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()
    assert host.config.global_lock.stat().st_ino == inode


def test_wrong_owner_global_parent_rejected_before_install(host, monkeypatch):
    parent = host.root / "wrong-owner-lock-parent"
    parent.mkdir()
    host.config.global_lock.rename(parent / "global.lock")
    host.config.global_lock = parent / "global.lock"
    original = Path.stat
    def metadata(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == parent:
            fields = list(value)
            fields[4] = 65534  # Only this external owner observation is injected on non-root macOS.
            return os.stat_result(fields)
        return value
    if os.geteuid() == 0:
        os.chown(parent, 65534, host.config.root_gid)
    else:
        monkeypatch.setattr(Path, "stat", metadata)
    result = host.module.execute(host.config, "install", None, host.runner)
    assert result["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()


def test_root_owned_sticky_global_parent_preserves_lock_and_allows_hold(host):
    parent = host.root / "sticky-lock-parent"
    parent.mkdir()
    parent.chmod(0o1777)
    host.config.global_lock.rename(parent / "global.lock")
    host.config.global_lock = parent / "global.lock"
    original = host.config.global_lock.stat()
    result = host.module.execute(host.config, "install", None, host.runner)
    assert result["result"] == "succeeded"
    after = host.config.global_lock.stat()
    assert (after.st_dev, after.st_ino) == (original.st_dev, original.st_ino)
    assert host.module.store_for(host.config).read_control().state == "hold"


def test_writable_global_ancestor_cannot_replace_a_safe_parent(host):
    ancestor = host.root / "unsafe-lock-ancestor"
    parent = ancestor / "safe-parent"
    parent.mkdir(parents=True)
    ancestor.chmod(0o777)
    host.config.global_lock.rename(parent / "global.lock")
    host.config.global_lock = parent / "global.lock"
    result = host.module.execute(host.config, "install", None, host.runner)
    assert result["result"] == "blocked"
    assert not host.config.bootstrap_root.exists()
