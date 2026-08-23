import csv
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.monitoring.resource_metrics import (
    CollectionUnavailable,
    HostSnapshot,
    ResourceSnapshot,
    UnitSnapshot,
    append_csv,
    atomic_write_json,
    collect_resource_snapshot,
    parse_meminfo,
    parse_systemctl_show,
    percent_used,
)
from ops.monitoring.alert_policy import advance_state, evaluate_rules


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_meminfo_converts_kilobytes_to_bytes():
    values = parse_meminfo((FIXTURES / "meminfo.txt").read_text())

    assert values["MemTotal"] == 16_777_216_000
    assert values["MemAvailable"] == 6_144_000_000
    assert values["SwapTotal"] == 2_147_483_648


def test_parse_systemctl_show_preserves_explicit_property_values():
    values = parse_systemctl_show((FIXTURES / "systemctl-show.txt").read_text())

    assert values == {
        "LoadState": "loaded",
        "UnitFileState": "enabled",
        "ActiveState": "active",
        "SubState": "running",
        "Result": "success",
        "MemoryCurrent": "1048576",
        "MemoryPeak": "2097152",
        "MemoryHigh": "max",
        "MemoryMax": "[not set]",
        "TasksCurrent": "",
        "TasksMax": "512",
        "CPUUsageNSec": "987654321",
        "NRestarts": "3",
        "ControlGroup": "/system.slice/legaltech.slice",
    }


@pytest.mark.parametrize(
    ("total", "available", "expected"),
    [
        (100, 25, 75.0),
        (0, 0, 0.0),
        (0, 10, 0.0),
    ],
)
def test_percent_used_handles_zero_totals(total, available, expected):
    assert percent_used(total, available) == expected


def test_collect_uses_memavailable_and_normalizes_systemd_limits():
    calls = []

    class FakeStatvfs:
        f_blocks = 1_000
        f_frsize = 4_096
        f_bfree = 250
        f_files = 500
        f_ffree = 125

    def run_command(command, timeout):
        calls.append((command, timeout))
        unit_name = command[4] if command[1] == "--user" else command[2]
        control_groups = {
            "legaltech.slice": "/legaltech.slice",
            "estrado-pjud.service": "/legaltech.slice/estrado-pjud.service",
            "estrado-pjud-worker.service": (
                "/legaltech.slice/estrado-pjud-worker.service"
            ),
            "legaltech-monitor.service": "/system.slice/legaltech-monitor.service",
            "legaltech-resource-tracker.service": (
                "/system.slice/legaltech-resource-tracker.service"
            ),
            "legaltech-monitor.timer": "/system.slice/legaltech-monitor.timer",
            "legaltech-resource-tracker.timer": (
                "/system.slice/legaltech-resource-tracker.timer"
            ),
            "user-4242.slice": "/user.slice/user-4242.slice",
            "hermes-gateway.service": (
                "/user.slice/user-4242.slice/user@4242.service/"
                "app.slice/hermes-gateway.service"
            ),
            "hermes-dashboard.service": (
                "/user.slice/user-4242.slice/user@4242.service/"
                "app.slice/hermes-dashboard.service"
            ),
        }
        return (FIXTURES / "systemctl-show.txt").read_text().replace(
            "/system.slice/legaltech.slice", control_groups[unit_name]
        )

    snapshot = collect_resource_snapshot(
        hermes_user_slice="user-4242.slice",
        read_text=lambda path: (FIXTURES / "meminfo.txt").read_text(),
        statvfs=lambda path: FakeStatvfs(),
        run_command=run_command,
        loadavg=lambda: (1.25, 0.0, 0.0),
        now=lambda: "2026-08-19T12:00:00Z",
    )

    assert snapshot.host.memory_available_bytes == 6_144_000_000
    assert snapshot.host.swap_used_bytes == 536_870_912
    assert snapshot.host.root_bytes_total == 4_096_000
    assert snapshot.host.root_bytes_used == 3_072_000
    assert snapshot.host.root_inodes_total == 500
    assert snapshot.host.root_inodes_used == 375
    assert snapshot.units["legaltech.slice"].memory_current_bytes == 1_048_576
    assert snapshot.units["legaltech.slice"].memory_high_bytes is None
    assert snapshot.units["legaltech.slice"].memory_max_bytes is None
    assert snapshot.units["legaltech.slice"].tasks_current is None
    assert snapshot.units["legaltech.slice"].tasks_max == 512
    assert snapshot.units["legaltech.slice"].cpu_usage_ns == 987_654_321
    assert set(snapshot.units) == {
        "legaltech.slice",
        "estrado-pjud.service",
        "estrado-pjud-worker.service",
        "legaltech-monitor.service",
        "legaltech-resource-tracker.service",
        "legaltech-monitor.timer",
        "legaltech-resource-tracker.timer",
        "user-4242.slice",
        "hermes-gateway.service",
        "hermes-dashboard.service",
    }
    assert len(calls) == 10
    assert all(timeout == 5.0 for _, timeout in calls)
    assert all("--property=ControlGroup" in command for command, _ in calls)
    assert all("--property=LoadState" in command for command, _ in calls)
    assert all("--property=UnitFileState" in command for command, _ in calls)
    assert all("--property=Result" in command for command, _ in calls)
    assert snapshot.units["legaltech-monitor.service"].result == "success"
    assert snapshot.units["user-4242.slice"].control_group == (
        "/user.slice/user-4242.slice"
    )
    hermes_commands = [command for command, _ in calls if command[1] == "--user"]
    assert [command[:5] for command in hermes_commands] == [
        [
            "systemctl",
            "--user",
            "--machine=hermes@.host",
            "show",
            "hermes-gateway.service",
        ],
        [
            "systemctl",
            "--user",
            "--machine=hermes@.host",
            "show",
            "hermes-dashboard.service",
        ],
    ]


def test_collector_wrong_hermes_cgroup_reaches_policy_and_suppresses_heartbeat():
    class FakeStatvfs:
        f_blocks = 100
        f_frsize = 1
        f_bfree = 50
        f_files = 100
        f_ffree = 50

    wrong_path = "/system.slice/hermes-gateway.service"

    def run_command(command, timeout):
        unit_name = command[4] if command[1] == "--user" else command[2]
        expected = {
            "legaltech.slice": "/legaltech.slice",
            "estrado-pjud.service": "/legaltech.slice/estrado-pjud.service",
            "estrado-pjud-worker.service": "/legaltech.slice/estrado-pjud-worker.service",
            "user-4242.slice": "/user.slice/user-4242.slice",
            "hermes-gateway.service": wrong_path,
            "hermes-dashboard.service": (
                "/user.slice/user-4242.slice/user@4242.service/"
                "app.slice/hermes-dashboard.service"
            ),
        }.get(unit_name, f"/system.slice/{unit_name}")
        return (FIXTURES / "systemctl-show.txt").read_text().replace(
            "/system.slice/legaltech.slice", expected
        )

    snapshot = collect_resource_snapshot(
        hermes_user_slice="user-4242.slice",
        read_text=lambda path: (FIXTURES / "meminfo.txt").read_text(),
        statvfs=lambda path: FakeStatvfs(),
        run_command=run_command,
        loadavg=lambda: (0.0, 0.0, 0.0),
        now=lambda: "2026-08-19T12:00:00Z",
    )
    events, _ = advance_state(
        evaluate_rules(snapshot, {}),
        {},
        datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
    )

    assert any(
        event.key == "unit.operational:hermes-gateway.service"
        for event in events
    )
    assert all(event.key != "healthy-heartbeat" for event in events)
    assert wrong_path not in json.dumps([event.__dict__ for event in events])


@pytest.mark.parametrize("failure_mode", ["producer", "duplicate-property"])
def test_hermes_user_manager_failure_is_sanitized_and_fails_closed(failure_mode):
    class FakeStatvfs:
        f_blocks = 100
        f_frsize = 1
        f_bfree = 50
        f_files = 100
        f_ffree = 50

    secret = "user-manager-secret-detail"

    def run_command(command, timeout):
        unit_name = command[4] if command[1] == "--user" else command[2]
        output = (FIXTURES / "systemctl-show.txt").read_text()
        if unit_name == "hermes-gateway.service":
            if failure_mode == "producer":
                raise RuntimeError(secret)
            output += "ActiveState=active\n"
        return output

    snapshot = collect_resource_snapshot(
        hermes_user_slice="user-4242.slice",
        read_text=lambda path: (FIXTURES / "meminfo.txt").read_text(),
        statvfs=lambda path: FakeStatvfs(),
        run_command=run_command,
        loadavg=lambda: (0.0, 0.0, 0.0),
        now=lambda: "2026-08-19T12:00:00Z",
    )
    failed = snapshot.units["hermes-gateway.service"]
    events, _ = advance_state(
        evaluate_rules(snapshot, {}),
        {},
        datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
    )

    assert failed.diagnostic == "systemctl show failed"
    assert secret not in repr(failed)
    assert any(
        event.key == "unit.operational:hermes-gateway.service"
        for event in events
    )
    assert all(event.key != "healthy-heartbeat" for event in events)


def test_collected_disabled_inactive_hermes_service_needs_no_live_cgroup():
    class FakeStatvfs:
        f_blocks = 100
        f_frsize = 1
        f_bfree = 50
        f_files = 100
        f_ffree = 50

    def run_command(command, timeout):
        unit_name = command[4] if command[1] == "--user" else command[2]
        control_group = {
            "legaltech.slice": "/legaltech.slice",
            "estrado-pjud.service": "/legaltech.slice/estrado-pjud.service",
            "estrado-pjud-worker.service": "/legaltech.slice/estrado-pjud-worker.service",
            "user-4242.slice": "/user.slice/user-4242.slice",
            "hermes-gateway.service": (
                "/user.slice/user-4242.slice/user@4242.service/"
                "app.slice/hermes-gateway.service"
            ),
        }.get(unit_name, f"/system.slice/{unit_name}")
        output = (FIXTURES / "systemctl-show.txt").read_text().replace(
            "/system.slice/legaltech.slice", control_group
        )
        if unit_name == "hermes-dashboard.service":
            output = (
                output.replace("UnitFileState=enabled", "UnitFileState=disabled")
                .replace("ActiveState=active", "ActiveState=inactive")
                .replace("SubState=running", "SubState=dead")
                .replace(
                    "ControlGroup=/system.slice/hermes-dashboard.service",
                    "ControlGroup=",
                )
            )
        return output

    snapshot = collect_resource_snapshot(
        hermes_user_slice="user-4242.slice",
        read_text=lambda path: (FIXTURES / "meminfo.txt").read_text(),
        statvfs=lambda path: FakeStatvfs(),
        run_command=run_command,
        loadavg=lambda: (0.0, 0.0, 0.0),
        now=lambda: "2026-08-19T12:00:00Z",
    )
    events, _ = advance_state(
        evaluate_rules(snapshot, {}),
        {},
        datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
    )

    assert snapshot.units["hermes-dashboard.service"].active_state == "inactive"
    assert snapshot.units["hermes-dashboard.service"].control_group is None
    assert [event.key for event in events] == ["healthy-heartbeat"]


def test_collect_keeps_host_metrics_when_one_unit_command_fails_without_leaking_error():
    secret_value = "do-not-disclose-this-environment-value"

    class FakeStatvfs:
        f_blocks = 10
        f_frsize = 1
        f_bfree = 4
        f_files = 10
        f_ffree = 4

    def run_command(command, timeout):
        if command[2] == "user-999.slice":
            raise RuntimeError(f"systemctl denied {secret_value}")
        return (FIXTURES / "systemctl-show.txt").read_text()

    snapshot = collect_resource_snapshot(
        hermes_user_slice="user-999.slice",
        read_text=lambda path: (FIXTURES / "meminfo.txt").read_text(),
        statvfs=lambda path: FakeStatvfs(),
        run_command=run_command,
        loadavg=lambda: (0.0, 0.0, 0.0),
        now=lambda: "2026-08-19T12:00:00Z",
    )

    failed_unit = snapshot.units["user-999.slice"]
    assert snapshot.host.memory_total_bytes == 16_777_216_000
    assert failed_unit.active_state == "unknown"
    assert failed_unit.sub_state == "unknown"
    assert failed_unit.load_state == "unknown"
    assert failed_unit.diagnostic == "systemctl show failed"
    assert secret_value not in failed_unit.diagnostic


@pytest.mark.parametrize("missing_key", ["MemTotal", "MemAvailable", "SwapTotal", "SwapFree"])
def test_collect_rejects_missing_required_meminfo_key_with_sanitized_typed_error(
    missing_key,
):
    secret = "must-not-leak-host-reader-detail"
    meminfo = "\n".join(
        line
        for line in (FIXTURES / "meminfo.txt").read_text().splitlines()
        if not line.startswith(f"{missing_key}:")
    )

    with pytest.raises(CollectionUnavailable) as raised:
        collect_resource_snapshot(
            hermes_user_slice="user-4242.slice",
            read_text=lambda path: meminfo,
            statvfs=lambda path: (_ for _ in ()).throw(
                AssertionError("statvfs must not run after invalid meminfo")
            ),
            run_command=lambda command, timeout: (_ for _ in ()).throw(
                AssertionError("systemd must not run after invalid meminfo")
            ),
            loadavg=lambda: (0.0, 0.0, 0.0),
        )

    assert str(raised.value) == "Required host resource metrics are unavailable"
    assert secret not in str(raised.value)


@pytest.mark.parametrize("failed_boundary", ["statvfs", "loadavg"])
def test_collect_wraps_statvfs_and_load_failures_as_sanitized_unavailable(
    failed_boundary,
):
    secret = "host-boundary-secret"

    class FakeStatvfs:
        f_blocks = 100
        f_frsize = 4096
        f_bfree = 50
        f_files = 100
        f_ffree = 50

    statvfs = (
        (lambda path: (_ for _ in ()).throw(OSError(secret)))
        if failed_boundary == "statvfs"
        else (lambda path: FakeStatvfs())
    )
    loadavg = (
        (lambda: (_ for _ in ()).throw(OSError(secret)))
        if failed_boundary == "loadavg"
        else (lambda: (0.1, 0.0, 0.0))
    )

    with pytest.raises(CollectionUnavailable) as raised:
        collect_resource_snapshot(
            hermes_user_slice="user-4242.slice",
            read_text=lambda path: (FIXTURES / "meminfo.txt").read_text(),
            statvfs=statvfs,
            run_command=lambda command, timeout: (FIXTURES / "systemctl-show.txt").read_text(),
            loadavg=loadavg,
        )

    assert str(raised.value) == "Required host resource metrics are unavailable"
    assert secret not in str(raised.value)


def test_atomic_write_json_replaces_value_without_leaving_a_temporary_file(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"previous": true}')

    atomic_write_json(target, {"schema_version": 1, "active": False})

    assert json.loads(target.read_text()) == {"schema_version": 1, "active": False}
    assert list(tmp_path.glob(".state.json.*")) == []


def test_atomic_write_json_fsyncs_the_parent_directory_after_replacement(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    synced_kinds = []
    real_fsync = os.fsync

    def recording_fsync(descriptor):
        synced_kinds.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr("ops.monitoring.resource_metrics.os.fsync", recording_fsync)

    atomic_write_json(target, {"schema_version": 1, "active": True})

    assert synced_kinds == [stat.S_IFREG, stat.S_IFDIR]
    assert json.loads(target.read_text()) == {"schema_version": 1, "active": True}
    assert list(tmp_path.glob(".state.json.*")) == []


def test_atomic_write_json_reports_directory_fsync_failure_without_leaking_details(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    target.write_text('{"previous":true}\n')
    secret = "directory-fsync-secret"
    real_fsync = os.fsync

    def failing_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(secret)
        real_fsync(descriptor)

    monkeypatch.setattr(
        "ops.monitoring.resource_metrics.os.fsync", failing_directory_fsync
    )

    with pytest.raises(OSError) as raised:
        atomic_write_json(target, {"schema_version": 1, "active": True})

    assert str(raised.value) == "Atomic JSON state write failed"
    assert secret not in str(raised.value)
    assert list(tmp_path.glob(".state.json.*")) == []


def test_atomic_write_json_cleans_temp_and_preserves_old_state_before_replace(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    target.write_text('{"previous":true}\n')
    secret = "temporary-write-secret"

    def fail_json_write(*args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr("ops.monitoring.resource_metrics.json.dump", fail_json_write)

    with pytest.raises(OSError) as raised:
        atomic_write_json(target, {"schema_version": 1, "active": True})

    assert str(raised.value) == "Atomic JSON state write failed"
    assert secret not in str(raised.value)
    assert json.loads(target.read_text()) == {"previous": True}
    assert list(tmp_path.glob(".state.json.*")) == []


def test_atomic_write_json_rejects_a_symlinked_parent_without_touching_target(tmp_path):
    real_directory = tmp_path / "real-state"
    real_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked-state"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(OSError) as raised:
        atomic_write_json(linked_directory / "state.json", {"schema_version": 1})

    assert str(raised.value) == "Atomic JSON state write failed"
    assert list(real_directory.iterdir()) == []


def test_atomic_write_json_rejects_a_directory_writable_by_other_users(tmp_path):
    unsafe_directory = tmp_path / "unsafe-state"
    unsafe_directory.mkdir()
    unsafe_directory.chmod(0o770)

    with pytest.raises(OSError) as raised:
        atomic_write_json(unsafe_directory / "state.json", {"schema_version": 1})

    assert str(raised.value) == "Atomic JSON state write failed"
    assert list(unsafe_directory.iterdir()) == []


def test_atomic_write_json_commits_private_single_link_state(tmp_path):
    target = tmp_path / "state.json"

    atomic_write_json(target, {"schema_version": 1})

    metadata = target.stat(follow_symlinks=False)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == os.geteuid()
    assert metadata.st_nlink == 1


@pytest.mark.parametrize(
    ("crash_timing", "expected_status"),
    [("before", 91), ("after", 92)],
)
def test_atomic_write_json_crash_at_directory_sync_never_reports_success_or_leaves_temp(
    tmp_path, crash_timing, expected_status
):
    target = tmp_path / "state.json"
    script = """
import os
import stat
from pathlib import Path
from ops.monitoring import resource_metrics

target = Path(os.environ["MONITOR_STATE_PATH"])
timing = os.environ["MONITOR_CRASH_TIMING"]
real_fsync = os.fsync

def crash_at_directory_sync(descriptor):
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
        if timing == "before":
            os._exit(91)
        real_fsync(descriptor)
        os._exit(92)
    real_fsync(descriptor)

resource_metrics.os.fsync = crash_at_directory_sync
resource_metrics.atomic_write_json(target, {"schema_version": 1, "active": True})
"""
    environment = os.environ.copy()
    environment.update(
        {
            "MONITOR_STATE_PATH": str(target),
            "MONITOR_CRASH_TIMING": crash_timing,
            "PYTHONPATH": str(Path(__file__).parents[3]),
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[3],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == expected_status
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert json.loads(target.read_text()) == {"schema_version": 1, "active": True}
    assert list(tmp_path.glob(".state.json.*")) == []


def test_append_csv_writes_versioned_stable_rows(tmp_path):
    snapshot = ResourceSnapshot(
        schema_version=1,
        timestamp_utc="2026-08-19T12:00:00Z",
        host=HostSnapshot(
            memory_total_bytes=1_000,
            memory_available_bytes=250,
            swap_total_bytes=500,
            swap_used_bytes=125,
            load_1m=1.5,
            root_bytes_total=2_000,
            root_bytes_used=400,
            root_inodes_total=300,
            root_inodes_used=60,
        ),
        units={
            "z.service": UnitSnapshot(
                name="z.service",
                active_state="active",
                sub_state="running",
                memory_current_bytes=10,
                memory_peak_bytes=20,
                memory_high_bytes=30,
                memory_max_bytes=40,
                tasks_current=2,
                tasks_max=4,
                cpu_usage_ns=50,
                n_restarts=1,
            ),
            "a.service": UnitSnapshot(
                name="a.service",
                active_state="inactive",
                sub_state="dead",
                memory_current_bytes=None,
                memory_peak_bytes=None,
                memory_high_bytes=None,
                memory_max_bytes=None,
                tasks_current=None,
                tasks_max=None,
                cpu_usage_ns=None,
                n_restarts=None,
            ),
        },
    )
    target = tmp_path / "resources.csv"

    append_csv(target, snapshot)

    rows = list(csv.reader(target.open(newline="")))
    assert rows[0] == [
        "schema_version",
        "timestamp_utc",
        "memory_total_bytes",
        "memory_available_bytes",
        "swap_total_bytes",
        "swap_used_bytes",
        "load_1m",
        "root_bytes_total",
        "root_bytes_used",
        "root_inodes_total",
        "root_inodes_used",
        "unit_name",
        "load_state",
        "unit_file_state",
        "active_state",
        "sub_state",
        "result",
        "control_group",
        "memory_current_bytes",
        "memory_peak_bytes",
        "memory_high_bytes",
        "memory_max_bytes",
        "tasks_current",
        "tasks_max",
        "cpu_usage_ns",
        "n_restarts",
    ]
    assert rows[1][0:18] == [
        "1",
        "2026-08-19T12:00:00Z",
        "1000",
        "250",
        "500",
        "125",
        "1.5",
        "2000",
        "400",
        "300",
        "60",
        "a.service",
        "loaded",
        "enabled",
        "inactive",
        "dead",
        "success",
        "",
    ]
    assert rows[2][11] == "z.service"
    assert rows[1][18:] == ["", "", "", "", "", "", "", ""]
