import csv
import json
from pathlib import Path

import pytest

from ops.monitoring.resource_metrics import (
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


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_meminfo_converts_kilobytes_to_bytes():
    values = parse_meminfo((FIXTURES / "meminfo.txt").read_text())

    assert values["MemTotal"] == 16_777_216_000
    assert values["MemAvailable"] == 6_144_000_000
    assert values["SwapTotal"] == 2_147_483_648


def test_parse_systemctl_show_preserves_explicit_property_values():
    values = parse_systemctl_show((FIXTURES / "systemctl-show.txt").read_text())

    assert values == {
        "ActiveState": "active",
        "SubState": "running",
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
        return (FIXTURES / "systemctl-show.txt").read_text()

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
        "user-4242.slice",
    }
    assert len(calls) == 6
    assert all(timeout == 5.0 for _, timeout in calls)
    assert all("--property=ControlGroup" in command for command, _ in calls)


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
    assert failed_unit.active_state == "inactive"
    assert failed_unit.sub_state == "unknown"
    assert failed_unit.diagnostic == "systemctl show failed"
    assert secret_value not in failed_unit.diagnostic


def test_atomic_write_json_replaces_value_without_leaving_a_temporary_file(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"previous": true}')

    atomic_write_json(target, {"schema_version": 1, "active": False})

    assert json.loads(target.read_text()) == {"schema_version": 1, "active": False}
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
        "active_state",
        "sub_state",
        "memory_current_bytes",
        "memory_peak_bytes",
        "memory_high_bytes",
        "memory_max_bytes",
        "tasks_current",
        "tasks_max",
        "cpu_usage_ns",
        "n_restarts",
    ]
    assert rows[1][0:14] == [
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
        "inactive",
        "dead",
    ]
    assert rows[2][11] == "z.service"
    assert rows[1][14:] == ["", "", "", "", "", "", "", ""]
