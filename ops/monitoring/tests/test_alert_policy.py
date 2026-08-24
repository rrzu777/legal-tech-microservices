from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ops.monitoring.alert_policy import (
    AlertEvent,
    advance_state,
    evaluate_rules,
    load_state,
    record_delivery_failure,
    record_delivery_success,
    save_state,
)
from ops.monitoring.resource_metrics import HostSnapshot, ResourceSnapshot, UnitSnapshot


UTC = timezone.utc


def unit(
    name: str,
    *,
    active_state: str = "active",
    memory_current: int = 10,
    memory_high: int = 100,
    restarts: int = 0,
    load_state: str = "loaded",
    unit_file_state: str = "enabled",
    result: str = "success",
    control_group: str | None = None,
) -> UnitSnapshot:
    return UnitSnapshot(
        name=name,
        active_state=active_state,
        sub_state="running" if active_state == "active" else "dead",
        memory_current_bytes=memory_current,
        memory_peak_bytes=memory_current,
        memory_high_bytes=memory_high,
        memory_max_bytes=200,
        tasks_current=1,
        tasks_max=10,
        cpu_usage_ns=1,
        n_restarts=restarts,
        load_state=load_state,
        unit_file_state=unit_file_state,
        result=result,
        control_group=control_group,
    )


def snapshot(
    *,
    memory_available: int = 50,
    swap_used: int = 0,
    root_bytes_used: int = 10,
    root_inodes_used: int = 10,
    slice_memory: int = 10,
    slice_high: int = 100,
    api_active: str = "active",
    worker_active: str = "active",
    api_restarts: int = 0,
    worker_restarts: int = 0,
    monitor_restarts: int = 0,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        schema_version=1,
        timestamp_utc="2026-08-19T12:00:00Z",
        host=HostSnapshot(
            memory_total_bytes=100,
            memory_available_bytes=memory_available,
            swap_total_bytes=100,
            swap_used_bytes=swap_used,
            load_1m=0.25,
            root_bytes_total=100,
            root_bytes_used=root_bytes_used,
            root_inodes_total=100,
            root_inodes_used=root_inodes_used,
            managed_swap_status="healthy",
        ),
        units={
            "legaltech.slice": unit(
                "legaltech.slice",
                memory_current=slice_memory,
                memory_high=slice_high,
                control_group="/legaltech.slice",
            ),
            "estrado-pjud.service": unit(
                "estrado-pjud.service",
                active_state=api_active,
                restarts=api_restarts,
                control_group=(
                    "/legaltech.slice/estrado-pjud.service"
                    if api_active == "active"
                    else None
                ),
            ),
            "estrado-pjud-worker.service": unit(
                "estrado-pjud-worker.service",
                active_state=worker_active,
                restarts=worker_restarts,
                control_group=(
                    "/legaltech.slice/estrado-pjud-worker.service"
                    if worker_active == "active"
                    else None
                ),
            ),
            "legaltech-monitor.service": unit(
                "legaltech-monitor.service",
                active_state="inactive",
                restarts=monitor_restarts,
            ),
            "legaltech-resource-tracker.service": unit(
                "legaltech-resource-tracker.service", active_state="inactive"
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


def rules_by_key(sample: ResourceSnapshot, state=None):
    return {rule.key: rule for rule in evaluate_rules(sample, state or {})}


def evaluate(sample, state, now):
    return advance_state(evaluate_rules(sample, state), state, now)


@pytest.mark.parametrize(
    ("sample", "key", "severity", "persist_for_seconds"),
    [
        (snapshot(api_active="inactive"), "unit.inactive:estrado-pjud.service", "critical", 0),
        (
            snapshot(worker_active="inactive"),
            "unit.inactive:estrado-pjud-worker.service",
            "critical",
            0,
        ),
        (snapshot(memory_available=14), "host.memory.available", "warning", 900),
        (snapshot(memory_available=7), "host.memory.available", "critical", 300),
        (snapshot(swap_used=26), "host.swap.used", "warning", 900),
        (snapshot(swap_used=51), "host.swap.used", "critical", 0),
        (snapshot(root_bytes_used=80), "host.root.bytes", "warning", 0),
        (snapshot(root_bytes_used=90), "host.root.bytes", "critical", 0),
        (snapshot(root_inodes_used=80), "host.root.inodes", "warning", 0),
        (snapshot(root_inodes_used=90), "host.root.inodes", "critical", 0),
        (snapshot(slice_memory=81), "slice.memory_high:legaltech.slice", "warning", 900),
    ],
)
def test_threshold_selects_one_highest_severity_per_stable_family(
    sample, key, severity, persist_for_seconds
):
    rule = rules_by_key(sample)[key]

    assert rule.active is True
    assert rule.severity == severity
    assert rule.persist_for_seconds == persist_for_seconds
    assert [candidate.key for candidate in evaluate_rules(sample, {})].count(key) == 1


def test_persistent_rule_fires_only_after_its_duration():
    start = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    early_events, state = evaluate(snapshot(memory_available=14), {}, start)
    almost_events, state = evaluate(
        snapshot(memory_available=14), state, start + timedelta(seconds=899)
    )
    due_events, _ = evaluate(
        snapshot(memory_available=14), state, start + timedelta(seconds=900)
    )

    assert early_events == []
    assert almost_events == []
    assert [(event.key, event.severity, event.kind) for event in due_events] == [
        ("host.memory.available", "warning", "firing")
    ]


def test_successful_delivery_owns_cooldown_and_failed_delivery_does_not_advance_it():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    events, state = evaluate(snapshot(api_active="inactive"), {}, now)
    event = events[0]

    failed = record_delivery_failure(state, event)
    retry_events, retry_state = evaluate(
        snapshot(api_active="inactive"), failed, now + timedelta(minutes=5)
    )
    sent = record_delivery_success(retry_state, retry_events[0], now + timedelta(minutes=5))
    cooling_events, sent = evaluate(
        snapshot(api_active="inactive"), sent, now + timedelta(hours=2)
    )
    due_events, _ = evaluate(
        snapshot(api_active="inactive"), sent, now + timedelta(hours=3, minutes=5)
    )

    entry = failed["rules"][event.key]
    assert entry["active_since"] == "2026-08-19T12:00:00Z"
    assert entry["last_sent_at"] is None
    assert entry["delivery_error"] == "Telegram delivery failed"
    assert retry_events == [event]
    assert cooling_events == []
    assert len(due_events) == 1


def test_recovery_emits_one_resolved_event_after_a_firing_was_delivered():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    firing, state = evaluate(snapshot(api_active="inactive"), {}, now)
    state = record_delivery_success(state, firing[0], now)

    resolved, state = evaluate(snapshot(), state, now + timedelta(minutes=5))
    state = record_delivery_success(state, resolved[0], now + timedelta(minutes=5))
    repeated, _ = evaluate(snapshot(), state, now + timedelta(minutes=10))

    assert [(event.key, event.kind) for event in resolved if event.kind == "resolved"] == [
        ("unit.inactive:estrado-pjud.service", "resolved")
    ]
    assert all(event.kind != "resolved" for event in repeated)


def test_new_critical_episode_fires_immediately_after_recent_resolution():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    firing, state = evaluate(snapshot(api_active="inactive"), {}, now)
    first = next(
        event
        for event in firing
        if event.key == "unit.inactive:estrado-pjud.service"
    )
    state = record_delivery_success(state, first, now)

    recovery, state = evaluate(snapshot(), state, now + timedelta(minutes=5))
    resolved = next(
        event
        for event in recovery
        if event.key == "unit.inactive:estrado-pjud.service"
        and event.kind == "resolved"
    )
    state = record_delivery_success(state, resolved, now + timedelta(minutes=5))

    relapse, _ = evaluate(
        snapshot(api_active="inactive"), state, now + timedelta(minutes=10)
    )

    assert [
        (event.key, event.severity, event.kind)
        for event in relapse
        if event.key == "unit.inactive:estrado-pjud.service"
    ] == [("unit.inactive:estrado-pjud.service", "critical", "firing")]


def test_restart_counter_establishes_baseline_then_warns_once_per_cooldown():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    baseline_events, state = evaluate(snapshot(api_restarts=4), {}, now)
    increased, state = evaluate(snapshot(api_restarts=5), state, now + timedelta(minutes=5))
    state = record_delivery_success(state, increased[0], now + timedelta(minutes=5))
    same_count, state = evaluate(snapshot(api_restarts=5), state, now + timedelta(minutes=10))
    suppressed, state = evaluate(snapshot(api_restarts=6), state, now + timedelta(hours=1))
    due, _ = evaluate(snapshot(api_restarts=6), state, now + timedelta(hours=3, minutes=5))

    assert all(not event.key.startswith("unit.restarts:") for event in baseline_events)
    assert [(event.key, event.severity) for event in increased] == [
        ("unit.restarts:estrado-pjud.service", "warning")
    ]
    assert all(not event.key.startswith("unit.restarts:") for event in same_count)
    assert suppressed == []
    assert len(due) == 1


def test_healthy_heartbeat_is_sent_at_most_once_per_utc_day():
    morning = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    first, state = evaluate(snapshot(), {}, morning)
    heartbeat = next(event for event in first if event.key == "healthy-heartbeat")
    state = record_delivery_success(state, heartbeat, morning)

    same_day, state = evaluate(snapshot(), state, morning + timedelta(hours=12))
    next_day, _ = evaluate(snapshot(), state, morning + timedelta(days=1))

    assert all(event.key != "healthy-heartbeat" for event in same_day)
    assert [event.key for event in next_day].count("healthy-heartbeat") == 1


def test_failed_heartbeat_delivered_after_midnight_counts_for_delivery_day():
    before_midnight = datetime(2026, 8, 19, 23, 55, tzinfo=UTC)
    first, state = evaluate(snapshot(), {}, before_midnight)
    heartbeat = next(event for event in first if event.key == "healthy-heartbeat")
    state = record_delivery_failure(state, heartbeat)

    after_midnight = before_midnight + timedelta(minutes=10)
    retry, state = evaluate(snapshot(), state, after_midnight)
    retry_heartbeat = next(event for event in retry if event.key == "healthy-heartbeat")
    state = record_delivery_success(state, retry_heartbeat, after_midnight)
    same_day, _ = evaluate(snapshot(), state, after_midnight + timedelta(hours=12))

    assert all(event.key != "healthy-heartbeat" for event in same_day)


def test_atomic_state_survives_a_new_policy_process(tmp_path):
    state_path = tmp_path / "state.json"
    start = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    _, candidate = evaluate(snapshot(memory_available=14), {}, start)

    save_state(state_path, candidate)
    restored = load_state(state_path)
    events, _ = evaluate(
        snapshot(memory_available=14), restored, start + timedelta(minutes=15)
    )

    assert restored["rules"]["host.memory.available"]["active_since"] == (
        "2026-08-19T12:00:00Z"
    )
    assert events == [
        AlertEvent(
            key="host.memory.available",
            severity="warning",
            message="Host available memory is below 15%",
            kind="firing",
        )
    ]
    assert list(tmp_path.glob(".state.json.*")) == []


@pytest.mark.parametrize(
    ("unit_name", "mutation"),
    [
        ("legaltech.slice", "missing"),
        ("legaltech.slice", "failed"),
        ("user-4242.slice", "not-found"),
        ("legaltech-monitor.timer", "not-found"),
        ("legaltech-resource-tracker.timer", "not-found"),
        ("legaltech-monitor.service", "failed-result"),
        ("legaltech-resource-tracker.service", "failed-result"),
    ],
)
def test_required_observer_failure_is_immediate_and_suppresses_healthy_heartbeat(
    unit_name, mutation
):
    sample = snapshot()
    if mutation == "missing":
        sample.units.pop(unit_name)
    elif mutation == "failed":
        sample.units[unit_name] = unit(unit_name, active_state="failed")
    elif mutation == "not-found":
        sample.units[unit_name] = unit(
            unit_name,
            active_state="inactive",
            load_state="not-found",
            unit_file_state="not-found",
            result="success",
        )
    else:
        sample.units[unit_name] = unit(
            unit_name,
            active_state="inactive",
            result="failed",
        )

    rule = rules_by_key(sample)[f"unit.operational:{unit_name}"]
    events, _ = evaluate(sample, {}, datetime(2026, 8, 19, 12, 0, tzinfo=UTC))

    assert rule.active is True
    assert rule.persist_for_seconds == 0
    assert rule.message == f"Required operational unit {unit_name} is unavailable"
    assert any(event.key == rule.key for event in events)
    assert all(event.key != "healthy-heartbeat" for event in events)


def test_disabled_inactive_worker_is_optional_but_enabled_inactive_worker_is_not():
    disabled = snapshot(worker_active="inactive")
    disabled.units["estrado-pjud-worker.service"] = unit(
        "estrado-pjud-worker.service",
        active_state="inactive",
        unit_file_state="disabled",
    )
    enabled = snapshot(worker_active="inactive")

    disabled_rules = rules_by_key(disabled)
    enabled_rules = rules_by_key(enabled)

    assert disabled_rules["unit.inactive:estrado-pjud-worker.service"].active is False
    assert enabled_rules["unit.inactive:estrado-pjud-worker.service"].active is True


@pytest.mark.parametrize(
    "control_group",
    [None, "", "   ", "legaltech.slice/unit.service", "/legaltech.slice/\nunit"],
)
@pytest.mark.parametrize(
    ("unit_name", "rule_key"),
    [
        ("legaltech.slice", "unit.operational:legaltech.slice"),
        (
            "estrado-pjud.service",
            "unit.inactive:estrado-pjud.service",
        ),
        (
            "estrado-pjud-worker.service",
            "unit.inactive:estrado-pjud-worker.service",
        ),
        ("user-4242.slice", "unit.operational:user-4242.slice"),
    ],
)
def test_invalid_required_continuous_unit_cgroup_is_immediate_and_blocks_heartbeat(
    unit_name, rule_key, control_group
):
    sample = snapshot()
    sample.units[unit_name] = replace(
        sample.units[unit_name], control_group=control_group
    )

    rule = rules_by_key(sample)[rule_key]
    events, _ = evaluate(sample, {}, datetime(2026, 8, 19, 12, 0, tzinfo=UTC))

    assert rule.active is True
    assert rule.persist_for_seconds == 0
    assert any(event.key == rule_key for event in events)
    assert all(event.key != "healthy-heartbeat" for event in events)


@pytest.mark.parametrize(
    ("unit_name", "rule_key", "wrong_control_group"),
    [
        (
            "legaltech.slice",
            "unit.operational:legaltech.slice",
            "/system.slice/legaltech.slice",
        ),
        (
            "estrado-pjud.service",
            "unit.inactive:estrado-pjud.service",
            "/system.slice/estrado-pjud.service",
        ),
        (
            "estrado-pjud-worker.service",
            "unit.inactive:estrado-pjud-worker.service",
            "/system.slice/estrado-pjud-worker.service",
        ),
        (
            "user-4242.slice",
            "unit.operational:user-4242.slice",
            "/user.slice/user-9999.slice",
        ),
        (
            "hermes-gateway.service",
            "unit.operational:hermes-gateway.service",
            "/user.slice/user-4242.slice/user@4242.service/app.slice/wrong.service",
        ),
        (
            "hermes-dashboard.service",
            "unit.operational:hermes-dashboard.service",
            "/system.slice/hermes-dashboard.service",
        ),
    ],
)
def test_wrong_valid_cgroup_raises_sanitized_alert_and_blocks_heartbeat(
    unit_name, rule_key, wrong_control_group
):
    sample = snapshot()
    sample.units[unit_name] = replace(
        sample.units[unit_name], control_group=wrong_control_group
    )

    rule = rules_by_key(sample)[rule_key]
    events, _ = evaluate(sample, {}, datetime(2026, 8, 19, 12, 0, tzinfo=UTC))

    assert rule.active is True
    assert rule.persist_for_seconds == 0
    assert wrong_control_group not in rule.message
    assert wrong_control_group != rule.value
    assert any(event.key == rule_key for event in events)
    assert all(wrong_control_group not in event.message for event in events)
    assert all(event.key != "healthy-heartbeat" for event in events)


def test_disabled_inactive_worker_does_not_require_a_cgroup_for_healthy_heartbeat():
    sample = snapshot(worker_active="inactive")
    sample.units["estrado-pjud-worker.service"] = replace(
        sample.units["estrado-pjud-worker.service"],
        unit_file_state="disabled",
        control_group=None,
    )

    events, _ = evaluate(sample, {}, datetime(2026, 8, 19, 12, 0, tzinfo=UTC))

    assert [event.key for event in events] == ["healthy-heartbeat"]


def test_timers_and_inactive_successful_oneshots_need_no_cgroup_for_heartbeat():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    sample = snapshot()
    units_without_cgroups = (
        "legaltech-monitor.timer",
        "legaltech-resource-tracker.timer",
        "legaltech-monitor.service",
        "legaltech-resource-tracker.service",
    )

    assert all(
        sample.units[name].control_group is None for name in units_without_cgroups
    )

    results = evaluate_rules(sample, {})
    events, _ = advance_state(results, {}, now)

    assert all(
        not result.active
        for result in results
        if result.key.startswith("unit.operational:")
    )
    assert [event.key for event in events] == ["healthy-heartbeat"]


def test_inactive_hermes_service_needs_no_cgroup_for_healthy_heartbeat():
    sample = snapshot()
    sample.units["hermes-dashboard.service"] = replace(
        sample.units["hermes-dashboard.service"],
        active_state="inactive",
        unit_file_state="disabled",
        control_group=None,
    )

    events, _ = evaluate(sample, {}, datetime(2026, 8, 19, 12, 0, tzinfo=UTC))

    assert [event.key for event in events] == ["healthy-heartbeat"]
