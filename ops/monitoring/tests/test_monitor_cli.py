import io
import json
import subprocess
import sys
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
)


MONITOR_PATH = Path(__file__).parents[1] / "monitor.py"
UTC = timezone.utc


def unit(name, active_state="active", restarts=0):
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
    )


def sample(api_active="active"):
    return ResourceSnapshot(
        schema_version=1,
        timestamp_utc="2026-08-19T12:00:00Z",
        host=HostSnapshot(100, 50, 100, 0, 0.1, 100, 10, 100, 10),
        units={
            "legaltech.slice": unit("legaltech.slice"),
            "estrado-pjud.service": unit("estrado-pjud.service", api_active),
            "estrado-pjud-worker.service": unit("estrado-pjud-worker.service"),
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
            "user-4242.slice": unit("user-4242.slice"),
        },
    )


class FakeTransport:
    def __init__(self, sent):
        self.sent = sent

    def send(self, message):
        self.sent.append(message)


def fixed_clock():
    return datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


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
