import io
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.monitoring.alert_policy import new_state
from ops.monitoring.monitor import main
from ops.monitoring.resource_metrics import (
    CollectionUnavailable,
    HostSnapshot,
    ResourceSnapshot,
    UnitSnapshot,
    collect_resource_snapshot,
)


MONITOR_PATH = Path(__file__).parents[1] / "monitor.py"
UTC = timezone.utc


def unit(name, active_state="active", restarts=0, control_group=None):
    return UnitSnapshot(
        name=name,
        active_state=active_state,
        sub_state="running" if active_state == "active" else "dead",
        memory_current_bytes=10,
        memory_peak_bytes=10,
        memory_high_bytes=100,
        memory_max_bytes=200,
        tasks_current=1,
        tasks_max=10,
        cpu_usage_ns=1,
        n_restarts=restarts,
        control_group=control_group,
    )


def sample(api_active="active"):
    return ResourceSnapshot(
        schema_version=1,
        timestamp_utc="2026-08-19T12:00:00Z",
        host=HostSnapshot(
            100, 50, 100, 0, 0.1, 100, 10, 100, 10, managed_swap_status="healthy"
        ),
        units={
            "legaltech.slice": unit(
                "legaltech.slice", control_group="/legaltech.slice"
            ),
            "estrado-pjud.service": unit(
                "estrado-pjud.service",
                api_active,
                control_group=(
                    "/legaltech.slice/estrado-pjud.service"
                    if api_active == "active"
                    else None
                ),
            ),
            "estrado-pjud-worker.service": unit(
                "estrado-pjud-worker.service",
                control_group="/legaltech.slice/estrado-pjud-worker.service",
            ),
            "legaltech-monitor.service": unit(
                "legaltech-monitor.service", "inactive"
            ),
            "legaltech-resource-tracker.service": unit(
                "legaltech-resource-tracker.service", "inactive"
            ),
            "legaltech-monitor.timer": unit("legaltech-monitor.timer"),
            "legaltech-resource-tracker.timer": unit(
                "legaltech-resource-tracker.timer"
            ),
            "user-4242.slice": unit(
                "user-4242.slice",
                control_group="/user.slice/user-4242.slice",
            ),
            "hermes-gateway.service": unit(
                "hermes-gateway.service",
                control_group=(
                    "/user.slice/user-4242.slice/user@4242.service/"
                    "app.slice/hermes-gateway.service"
                ),
            ),
            "hermes-dashboard.service": unit(
                "hermes-dashboard.service",
                control_group=(
                    "/user.slice/user-4242.slice/user@4242.service/"
                    "app.slice/hermes-dashboard.service"
                ),
            ),
        },
        hermes_user_slice="user-4242.slice",
    )


@pytest.mark.parametrize(
    ("status", "expects_alert"),
    [
        ("missing", True),
        ("undersized", True),
        ("wrong-target", True),
        ("invalid", True),
        ("healthy", False),
    ],
)
def test_dry_run_managed_swap_contract_controls_alert_and_heartbeat(
    tmp_path, status, expects_alert
):
    output = io.StringIO()
    collected = sample()
    collected = replace(
        collected,
        host=replace(collected.host, managed_swap_status=status),
    )

    result = main(
        ["--dry-run", "--state-dir", str(tmp_path)],
        environ={},
        collect=lambda **kwargs: collected,
        clock=fixed_clock,
        state_loader=lambda path: new_state(),
        state_writer=lambda *args: (_ for _ in ()).throw(
            AssertionError("dry-run mutated state")
        ),
        transport_factory=lambda *args: (_ for _ in ()).throw(
            AssertionError("dry-run constructed transport")
        ),
        slice_resolver=lambda: "user-4242.slice",
        stdout=output,
        stderr=io.StringIO(),
    )

    rendered = json.loads(output.getvalue())
    event_keys = [event["key"] for event in rendered["events"]]
    assert result == 0
    assert ("host.swap.managed" in event_keys) is expects_alert
    assert ("healthy-heartbeat" in event_keys) is not expects_alert
    assert all("/swap" not in event["message"] for event in rendered["events"])


class FakeTransport:
    def __init__(self, sent):
        self.sent = sent

    def send(self, message):
        self.sent.append(message)


def fixed_clock():
    return datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def local_options(output, errors, **overrides):
    options = dict(
        environ={}, collect=lambda **kwargs: sample(api_active="inactive"),
        clock=fixed_clock, slice_resolver=lambda: "user-4242.slice",
        stdout=output, stderr=errors,
        transport_factory=lambda *args: pytest.fail("local mode touched Telegram"),
    )
    options.update(overrides)
    return options


def test_local_once_persists_separate_state_without_reading_credentials(tmp_path):
    class ForbiddenCredentials(dict):
        def get(self, *args):
            pytest.fail("local mode read credential environment")

    output, errors = io.StringIO(), io.StringIO()
    result = main(["--once", "--delivery", "local", "--state-dir", str(tmp_path)],
                  **local_options(output, errors, environ=ForbiddenCredentials()))
    assert result == 0
    assert errors.getvalue() == ""
    assert json.loads(output.getvalue())["delivery_mode"] == "local"
    state = json.loads((tmp_path / "state-local.json").read_text())
    assert state["delivery_mode"] == "local"
    assert state["rules"]["unit.inactive:estrado-pjud.service"]["pending"] is None
    assert not (tmp_path / "state.json").exists()


def test_local_ack_never_suppresses_later_telegram_delivery(tmp_path):
    options = local_options(io.StringIO(), io.StringIO())
    assert main(["--once", "--delivery", "local", "--state-dir", str(tmp_path)], **options) == 0
    sent = []
    options.update(environ={"LEGALTECH_TELEGRAM_BOT_TOKEN": "fixture-token",
                            "LEGALTECH_TELEGRAM_CHAT_ID": "fixture-chat"},
                   transport_factory=lambda *args: FakeTransport(sent), stdout=io.StringIO())
    assert main(["--once", "--delivery", "telegram", "--state-dir", str(tmp_path)], **options) == 0
    assert len(sent) == 1
    assert (tmp_path / "state.json").exists()


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_local_output_failure_retains_pending_event_and_returns_failure(tmp_path, failure):
    class BrokenOutput(io.StringIO):
        def write(self, value):
            if failure == "write":
                raise OSError("sensitive-output-detail")
            return super().write(value)

        def flush(self):
            if failure == "flush":
                raise OSError("sensitive-output-detail")

    errors = io.StringIO()
    assert main(["--once", "--delivery", "local", "--state-dir", str(tmp_path)],
                **local_options(BrokenOutput(), errors)) == 1
    state = json.loads((tmp_path / "state-local.json").read_text())
    entry = state["rules"]["unit.inactive:estrado-pjud.service"]
    assert entry["pending"] is not None
    assert entry["last_sent_at"] is None
    assert errors.getvalue() == "Local monitoring output failed\n"


def test_local_dry_run_has_no_state_or_delivery_effect(tmp_path):
    output, errors = io.StringIO(), io.StringIO()
    assert main(["--dry-run", "--delivery", "local", "--state-dir", str(tmp_path)],
                **local_options(output, errors)) == 0
    assert json.loads(output.getvalue())["delivery_mode"] == "local"
    assert not list(tmp_path.iterdir())


def test_local_dry_run_output_error_is_sanitized_without_state(tmp_path):
    class BrokenOutput(io.StringIO):
        def write(self, value):
            raise OSError("private-path-detail")

    errors = io.StringIO()
    assert main(["--dry-run", "--delivery", "local", "--state-dir", str(tmp_path)],
                **local_options(BrokenOutput(), errors)) == 1
    assert errors.getvalue() == "Monitoring output failed\n"
    assert not list(tmp_path.iterdir())


def test_local_rejects_external_test_alert(tmp_path):
    output, errors = io.StringIO(), io.StringIO()
    assert main(["--test-alert", "--delivery", "local", "--state-dir", str(tmp_path)],
                **local_options(output, errors)) == 2
    assert errors.getvalue() == "Synthetic Telegram alert requires telegram delivery mode\n"
    assert not list(tmp_path.iterdir())


def test_local_output_precedes_ack_and_ack_failure_is_reported(tmp_path):
    output, errors = io.StringIO(), io.StringIO()
    writes = []

    def writer(path, state):
        if writes:
            assert json.loads(output.getvalue())["delivery_mode"] == "local"
            raise OSError("private storage detail")
        assert output.getvalue() == ""
        writes.append(json.loads(json.dumps(state)))

    assert main(["--once", "--delivery", "local", "--state-dir", str(tmp_path)],
                **local_options(output, errors, state_writer=writer)) == 1
    assert writes[0]["rules"]["unit.inactive:estrado-pjud.service"]["pending"] is not None
    assert errors.getvalue() == "Monitoring state write failed\n"


def test_local_cooldown_is_persistent_and_empty_evaluation_still_visible(tmp_path):
    for expected in (1, 0):
        output, errors = io.StringIO(), io.StringIO()
        assert main(["--once", "--delivery", "local", "--state-dir", str(tmp_path)],
                    **local_options(output, errors)) == 0
        assert len(json.loads(output.getvalue())["events"]) == expected
        assert errors.getvalue() == ""


def test_once_persists_candidate_before_delivery_then_records_success(tmp_path):
    writes = []
    sent = []
    output = io.StringIO()

    result = main(
        ["--once", "--state-dir", str(tmp_path)],
        environ={
            "LEGALTECH_TELEGRAM_BOT_TOKEN": "token-from-env",
            "LEGALTECH_TELEGRAM_CHAT_ID": "chat-from-env",
        },
        collect=lambda **kwargs: sample(api_active="inactive"),
        clock=fixed_clock,
        state_loader=lambda path: new_state(),
        state_writer=lambda path, state: writes.append(json.loads(json.dumps(state))),
        transport_factory=lambda token, chat_id: FakeTransport(sent),
        slice_resolver=lambda: "user-4242.slice",
        stdout=output,
        stderr=io.StringIO(),
    )

    key = "unit.inactive:estrado-pjud.service"
    assert result == 0
    assert len(sent) == 1
    assert writes[0]["rules"][key]["last_sent_at"] is None
    assert writes[-1]["rules"][key]["last_sent_at"] == "2026-08-19T12:00:00Z"
    assert json.loads(output.getvalue())["events"][0]["kind"] == "firing"


def test_failed_delivery_preserves_retry_state_and_sanitized_error(tmp_path):
    writes = []
    stderr = io.StringIO()

    class FailingTransport:
        def send(self, message):
            raise RuntimeError("token-from-env chat-from-env payload-secret")

    result = main(
        ["--once", "--state-dir", str(tmp_path)],
        environ={
            "LEGALTECH_TELEGRAM_BOT_TOKEN": "token-from-env",
            "LEGALTECH_TELEGRAM_CHAT_ID": "chat-from-env",
        },
        collect=lambda **kwargs: sample(api_active="inactive"),
        clock=fixed_clock,
        state_loader=lambda path: new_state(),
        state_writer=lambda path, state: writes.append(json.loads(json.dumps(state))),
        transport_factory=lambda token, chat_id: FailingTransport(),
        slice_resolver=lambda: "user-4242.slice",
        stdout=io.StringIO(),
        stderr=stderr,
    )

    entry = writes[-1]["rules"]["unit.inactive:estrado-pjud.service"]
    assert result == 1
    assert entry["active_since"] == "2026-08-19T12:00:00Z"
    assert entry["last_sent_at"] is None
    assert entry["delivery_error"] == "Telegram delivery failed"
    assert "token-from-env" not in json.dumps(writes)
    assert "chat-from-env" not in json.dumps(writes)
    assert "payload-secret" not in json.dumps(writes)
    assert stderr.getvalue() == "Telegram delivery failed\n"


def test_initial_directory_sync_failure_prevents_delivery_and_success_output(
    tmp_path, monkeypatch
):
    sent = []
    stdout = io.StringIO()
    stderr = io.StringIO()
    secret = "initial-dir-sync-secret"
    real_fsync = os.fsync

    def failing_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(secret)
        real_fsync(descriptor)

    monkeypatch.setattr(
        "ops.monitoring.resource_metrics.os.fsync", failing_directory_fsync
    )

    result = main(
        ["--once", "--state-dir", str(tmp_path)],
        environ={
            "LEGALTECH_TELEGRAM_BOT_TOKEN": "token-from-env",
            "LEGALTECH_TELEGRAM_CHAT_ID": "chat-from-env",
        },
        collect=lambda **kwargs: sample(api_active="inactive"),
        clock=fixed_clock,
        transport_factory=lambda token, chat_id: FakeTransport(sent),
        slice_resolver=lambda: "user-4242.slice",
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 1
    assert sent == []
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Monitoring state write failed\n"
    assert secret not in stderr.getvalue()
    assert list(tmp_path.glob(".state.json.*")) == []


def test_delivery_directory_sync_failure_does_not_confirm_cooldown_or_success(
    tmp_path, monkeypatch
):
    sent = []
    stdout = io.StringIO()
    stderr = io.StringIO()
    real_fsync = os.fsync
    directory_syncs = 0

    def fail_second_directory_fsync(descriptor):
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
            if directory_syncs == 2:
                raise OSError("post-delivery-dir-sync-secret")
        real_fsync(descriptor)

    monkeypatch.setattr(
        "ops.monitoring.resource_metrics.os.fsync", fail_second_directory_fsync
    )

    result = main(
        ["--once", "--state-dir", str(tmp_path)],
        environ={
            "LEGALTECH_TELEGRAM_BOT_TOKEN": "token-from-env",
            "LEGALTECH_TELEGRAM_CHAT_ID": "chat-from-env",
        },
        collect=lambda **kwargs: sample(api_active="inactive"),
        clock=fixed_clock,
        transport_factory=lambda token, chat_id: FakeTransport(sent),
        slice_resolver=lambda: "user-4242.slice",
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 1
    assert len(sent) == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Monitoring state write failed\n"
    assert "post-delivery-dir-sync-secret" not in stderr.getvalue()
    assert list(tmp_path.glob(".state.json.*")) == []


def test_dry_run_returns_candidates_without_network_or_state_mutation(tmp_path):
    output = io.StringIO()

    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run mutated state or constructed transport")

    result = main(
        ["--dry-run", "--state-dir", str(tmp_path)],
        environ={
            "LEGALTECH_TELEGRAM_BOT_TOKEN": "present-but-unused",
            "LEGALTECH_TELEGRAM_CHAT_ID": "present-but-unused",
        },
        collect=lambda **kwargs: sample(api_active="inactive"),
        clock=fixed_clock,
        state_loader=lambda path: new_state(),
        state_writer=forbidden,
        transport_factory=forbidden,
        slice_resolver=lambda: "user-4242.slice",
        stdout=output,
        stderr=io.StringIO(),
    )

    rendered = json.loads(output.getvalue())
    assert result == 0
    assert rendered["dry_run"] is True
    assert rendered["events"][0]["key"] == "unit.inactive:estrado-pjud.service"
    assert list(tmp_path.iterdir()) == []


def test_dry_run_wrong_cgroup_is_sanitized_and_suppresses_heartbeat(tmp_path):
    output = io.StringIO()
    wrong_path = "/system.slice/hermes-gateway.service"

    class FakeStatvfs:
        f_blocks = 100
        f_frsize = 1
        f_bfree = 50
        f_files = 100
        f_ffree = 50

    properties = {
        "LoadState": "loaded",
        "UnitFileState": "enabled",
        "ActiveState": "active",
        "SubState": "running",
        "Result": "success",
        "MemoryCurrent": "10",
        "MemoryPeak": "10",
        "MemoryHigh": "100",
        "MemoryMax": "200",
        "TasksCurrent": "1",
        "TasksMax": "10",
        "CPUUsageNSec": "1",
        "NRestarts": "0",
    }

    def run_command(command, timeout):
        unit_name = command[4] if command[1] == "--user" else command[2]
        control_group = {
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
        return "\n".join(
            f"{key}={value}"
            for key, value in {**properties, "ControlGroup": control_group}.items()
        )

    collected = collect_resource_snapshot(
        hermes_user_slice="user-4242.slice",
        read_text=lambda path: (
            "Filename\tType\tSize\tUsed\tPriority\n"
            "/swapfile\tfile\t100\t0\t-2\n"
            if path == "/proc/swaps"
            else (
                "MemTotal: 100 kB\nMemAvailable: 50 kB\n"
                "SwapTotal: 100 kB\nSwapFree: 100 kB\n"
            )
        ),
        statvfs=lambda path: FakeStatvfs(),
        run_command=run_command,
        loadavg=lambda: (0.0, 0.0, 0.0),
        now=lambda: "2026-08-19T12:00:00Z",
    )

    result = main(
        ["--dry-run", "--state-dir", str(tmp_path)],
        environ={},
        collect=lambda **kwargs: collected,
        clock=fixed_clock,
        state_loader=lambda path: new_state(),
        state_writer=lambda *args: (_ for _ in ()).throw(
            AssertionError("dry-run mutated state")
        ),
        transport_factory=lambda *args: (_ for _ in ()).throw(
            AssertionError("dry-run constructed transport")
        ),
        slice_resolver=lambda: "user-4242.slice",
        stdout=output,
        stderr=io.StringIO(),
    )

    rendered = json.loads(output.getvalue())
    serialized = json.dumps(rendered)
    assert result == 0
    assert any(
        event["key"] == "unit.operational:hermes-gateway.service"
        for event in rendered["events"]
    )
    assert all(event["key"] != "healthy-heartbeat" for event in rendered["events"])
    assert wrong_path not in serialized


def test_collection_unavailable_becomes_stable_immediate_alert_without_heartbeat(
    tmp_path,
):
    output = io.StringIO()

    def unavailable(**kwargs):
        raise CollectionUnavailable("Required host resource metrics are unavailable")

    def forbidden(*args, **kwargs):
        raise AssertionError("collection failure dry-run mutated state or used network")

    result = main(
        ["--dry-run", "--state-dir", str(tmp_path)],
        environ={},
        collect=unavailable,
        clock=fixed_clock,
        state_loader=lambda path: new_state(),
        state_writer=forbidden,
        transport_factory=forbidden,
        slice_resolver=lambda: "user-4242.slice",
        stdout=output,
        stderr=io.StringIO(),
    )

    rendered = json.loads(output.getvalue())
    assert result == 0
    assert [event["key"] for event in rendered["events"]] == [
        "monitor.collection.unavailable"
    ]
    assert rendered["events"][0]["severity"] == "critical"
    assert all(event["key"] != "healthy-heartbeat" for event in rendered["events"])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"LEGALTECH_TELEGRAM_BOT_TOKEN": "only-token"},
        {"LEGALTECH_TELEGRAM_CHAT_ID": "only-chat"},
    ],
)
def test_test_alert_requires_both_environment_credentials(environ):
    result = main(
        ["--test-alert"],
        environ=environ,
        transport_factory=lambda *args: (_ for _ in ()).throw(
            AssertionError("transport must not be constructed")
        ),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert result == 2


def test_test_alert_is_clearly_synthetic_and_uses_environment_credentials():
    constructed = []
    sent = []

    def factory(token, chat_id):
        constructed.append((token, chat_id))
        return FakeTransport(sent)

    result = main(
        ["--test-alert"],
        environ={
            "LEGALTECH_TELEGRAM_BOT_TOKEN": "token-from-env",
            "LEGALTECH_TELEGRAM_CHAT_ID": "chat-from-env",
        },
        transport_factory=factory,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert result == 0
    assert constructed == [("token-from-env", "chat-from-env")]
    assert sent == ["JurisTrack synthetic monitoring test"]


def test_cli_rejects_secret_arguments_without_echoing_their_values():
    secret = "must-not-echo-this-cli-token"
    stderr = io.StringIO()

    result = main(
        ["--test-alert", "--token", secret],
        environ={},
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert result == 2
    assert secret not in stderr.getvalue()


def test_monitor_runs_as_a_flat_installed_script_without_repo_pythonpath():
    result = subprocess.run(
        [sys.executable, str(MONITOR_PATH), "--help"],
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert "--state-dir" in result.stdout


def test_flat_installed_cli_rejects_once_combined_with_dry_run():
    result = subprocess.run(
        [sys.executable, str(MONITOR_PATH), "--once", "--dry-run"],
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "Invalid monitoring arguments\n"
