"""Pure resource alert rules and persistent transition state."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from .resource_metrics import ResourceSnapshot, atomic_write_json
except ImportError:  # Flat installation in /opt/legaltech-monitoring.
    from resource_metrics import ResourceSnapshot, atomic_write_json


STATE_SCHEMA_VERSION = 1
DEFAULT_COOLDOWN_SECONDS = 3 * 60 * 60
CRITICAL_UNITS = ("estrado-pjud.service", "estrado-pjud-worker.service")
RESTART_UNITS = (
    "estrado-pjud.service",
    "estrado-pjud-worker.service",
    "legaltech-monitor.service",
)


@dataclass(frozen=True)
class RuleResult:
    key: str
    severity: str
    active: bool
    persist_for_seconds: int
    cooldown_seconds: int
    message: str
    value: int | float | str | None = None
    resolution_enabled: bool = True


@dataclass(frozen=True)
class AlertEvent:
    key: str
    severity: str
    message: str
    kind: str


def new_state() -> dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "rules": {}}


def evaluate_rules(
    snapshot: ResourceSnapshot, state: dict[str, Any] | None = None
) -> list[RuleResult]:
    """Evaluate one highest-severity result for each stable alert family."""
    previous_rules = (state or {}).get("rules", {})
    results: list[RuleResult] = []

    for unit_name in CRITICAL_UNITS:
        unit = snapshot.units.get(unit_name)
        inactive = unit is None or unit.active_state != "active"
        results.append(
            RuleResult(
                key=f"unit.inactive:{unit_name}",
                severity="critical",
                active=inactive,
                persist_for_seconds=0,
                cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
                message=f"Critical unit {unit_name} is inactive",
                value=unit.active_state if unit else "missing",
            )
        )

    host = snapshot.host
    available_percent = _percent(host.memory_available_bytes, host.memory_total_bytes)
    if host.memory_total_bytes > 0 and available_percent < 8:
        memory_severity, memory_duration, memory_message = (
            "critical",
            300,
            "Host available memory is below 8%",
        )
        memory_active = True
    elif host.memory_total_bytes > 0 and available_percent < 15:
        memory_severity, memory_duration, memory_message = (
            "warning",
            900,
            "Host available memory is below 15%",
        )
        memory_active = True
    else:
        memory_severity, memory_duration, memory_message = (
            "warning",
            900,
            "Host available memory recovered",
        )
        memory_active = False
    results.append(
        RuleResult(
            key="host.memory.available",
            severity=memory_severity,
            active=memory_active,
            persist_for_seconds=memory_duration,
            cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
            message=memory_message,
            value=available_percent,
        )
    )

    swap_percent = _percent(host.swap_used_bytes, host.swap_total_bytes)
    if host.swap_total_bytes > 0 and swap_percent > 50:
        swap_severity, swap_duration, swap_message = (
            "critical",
            0,
            "Host swap use is above 50%",
        )
        swap_active = True
    elif host.swap_total_bytes > 0 and swap_percent > 25:
        swap_severity, swap_duration, swap_message = (
            "warning",
            900,
            "Host swap use is above 25%",
        )
        swap_active = True
    else:
        swap_severity, swap_duration, swap_message = (
            "warning",
            900,
            "Host swap use recovered",
        )
        swap_active = False
    results.append(
        RuleResult(
            key="host.swap.used",
            severity=swap_severity,
            active=swap_active,
            persist_for_seconds=swap_duration,
            cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
            message=swap_message,
            value=swap_percent,
        )
    )

    results.append(
        _capacity_rule(
            key="host.root.bytes",
            used=host.root_bytes_used,
            total=host.root_bytes_total,
            label="Root filesystem bytes",
        )
    )
    results.append(
        _capacity_rule(
            key="host.root.inodes",
            used=host.root_inodes_used,
            total=host.root_inodes_total,
            label="Root filesystem inodes",
        )
    )

    slice_unit = snapshot.units.get("legaltech.slice")
    slice_ratio = None
    if (
        slice_unit is not None
        and slice_unit.memory_current_bytes is not None
        and slice_unit.memory_high_bytes is not None
        and slice_unit.memory_high_bytes > 0
    ):
        slice_ratio = _percent(
            slice_unit.memory_current_bytes, slice_unit.memory_high_bytes
        )
    results.append(
        RuleResult(
            key="slice.memory_high:legaltech.slice",
            severity="warning",
            active=slice_ratio is not None and slice_ratio > 80,
            persist_for_seconds=900,
            cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
            message=(
                "legaltech.slice memory use is above 80% of MemoryHigh"
                if slice_ratio is not None and slice_ratio > 80
                else "legaltech.slice memory use recovered"
            ),
            value=slice_ratio,
        )
    )

    for unit_name in RESTART_UNITS:
        unit = snapshot.units.get(unit_name)
        current = unit.n_restarts if unit is not None else None
        key = f"unit.restarts:{unit_name}"
        previous = previous_rules.get(key, {})
        last_value = previous.get("last_value")
        increased = (
            isinstance(current, int)
            and isinstance(last_value, int)
            and current > last_value
        )
        waiting_for_delivery = bool(previous.get("active_since")) and not bool(
            previous.get("notified")
        )
        results.append(
            RuleResult(
                key=key,
                severity="warning",
                active=increased or waiting_for_delivery,
                persist_for_seconds=0,
                cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
                message=f"{unit_name} restart count increased",
                value=current,
                resolution_enabled=False,
            )
        )

    return results


def advance_state(
    results: Iterable[RuleResult],
    state: dict[str, Any] | None,
    now: datetime,
) -> tuple[list[AlertEvent], dict[str, Any]]:
    """Return candidate events and a new state without mutating the input."""
    now = _require_utc(now)
    timestamp = _format_time(now)
    candidate = copy.deepcopy(state) if state else new_state()
    candidate["schema_version"] = STATE_SCHEMA_VERSION
    rules_state = candidate.setdefault("rules", {})
    events: list[AlertEvent] = []
    evaluated = list(results)

    for result in evaluated:
        entry = rules_state.setdefault(result.key, _empty_entry())
        was_active = entry.get("active_since") is not None
        previous_severity = entry.get("last_severity")
        entry["last_value"] = result.value
        entry["resolution_enabled"] = result.resolution_enabled

        if result.active:
            new_episode = not was_active
            severity_changed = was_active and previous_severity != result.severity
            if not was_active or severity_changed:
                entry["active_since"] = timestamp
                entry["last_severity"] = result.severity
                entry["last_message"] = result.message
                entry["notified"] = False
                entry["pending"] = None
            else:
                entry["last_message"] = result.message

            active_since = _parse_time(entry["active_since"])
            persisted = (now - active_since).total_seconds() >= result.persist_for_seconds
            pending = entry.get("pending")
            if pending and pending.get("kind") == "firing":
                events.append(_event_from_pending(result.key, pending))
                continue

            last_sent = _parse_optional_time(entry.get("last_sent_at"))
            cooldown_due = (
                last_sent is None
                or (now - last_sent).total_seconds() >= result.cooldown_seconds
            )
            should_send = persisted and (
                (
                    not entry.get("notified")
                    and (
                        (new_episode and result.resolution_enabled)
                        or cooldown_due
                        or severity_changed
                    )
                )
                or (entry.get("notified") and cooldown_due)
            )
            if should_send:
                event = AlertEvent(
                    key=result.key,
                    severity=result.severity,
                    message=result.message,
                    kind="firing",
                )
                entry["pending"] = _pending_from_event(event)
                events.append(event)
            continue

        pending = entry.get("pending")
        if pending and pending.get("kind") == "resolved":
            events.append(_event_from_pending(result.key, pending))
        elif was_active and entry.get("notified") and result.resolution_enabled:
            event = AlertEvent(
                key=result.key,
                severity=previous_severity or result.severity,
                message=f"Resolved: {entry.get('last_message') or result.message}",
                kind="resolved",
            )
            entry["pending"] = _pending_from_event(event)
            events.append(event)
        else:
            entry["pending"] = None
        entry["active_since"] = None
        entry["last_severity"] = None
        entry["notified"] = False

    unhealthy = any(result.active for result in evaluated)
    heartbeat = rules_state.setdefault("healthy-heartbeat", _empty_entry())
    today = now.date().isoformat()
    if unhealthy:
        heartbeat["pending"] = None
    elif heartbeat.get("last_value") != today:
        pending = heartbeat.get("pending")
        if pending is None:
            event = AlertEvent(
                key="healthy-heartbeat",
                severity="info",
                message="JurisTrack resource monitoring is healthy",
                kind="firing",
            )
            heartbeat["active_since"] = timestamp
            heartbeat["last_severity"] = "info"
            heartbeat["last_message"] = event.message
            heartbeat["pending"] = _pending_from_event(event) | {"date": today}
        else:
            event = _event_from_pending("healthy-heartbeat", pending)
        events.append(event)

    return events, candidate


def record_delivery_success(
    state: dict[str, Any], event: AlertEvent, now: datetime
) -> dict[str, Any]:
    """Record cooldown only after confirmed delivery."""
    updated = copy.deepcopy(state)
    entry = updated["rules"][event.key]
    entry["last_sent_at"] = _format_time(_require_utc(now))
    entry["delivery_error"] = None
    entry["pending"] = None
    if event.key == "healthy-heartbeat":
        entry["last_value"] = now.astimezone(timezone.utc).date().isoformat()
        entry["active_since"] = None
        entry["last_severity"] = None
        entry["notified"] = False
    elif event.kind == "firing":
        entry["notified"] = True
    return updated


def record_delivery_failure(
    state: dict[str, Any], event: AlertEvent
) -> dict[str, Any]:
    """Keep a retryable candidate while storing only a fixed safe error."""
    updated = copy.deepcopy(state)
    entry = updated["rules"][event.key]
    entry["delivery_error"] = "Telegram delivery failed"
    return updated


def load_state(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return new_state()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Monitoring state could not be loaded") from None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("rules"), dict):
        raise ValueError("Monitoring state could not be loaded")
    return loaded


def save_state(path: Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state)


def _capacity_rule(*, key: str, used: int, total: int, label: str) -> RuleResult:
    usage = _percent(used, total)
    if total > 0 and usage >= 90:
        severity, active, message = "critical", True, f"{label} use is at least 90%"
    elif total > 0 and usage >= 80:
        severity, active, message = "warning", True, f"{label} use is at least 80%"
    else:
        severity, active, message = "warning", False, f"{label} use recovered"
    return RuleResult(
        key=key,
        severity=severity,
        active=active,
        persist_for_seconds=0,
        cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
        message=message,
        value=usage,
    )


def _empty_entry() -> dict[str, Any]:
    return {
        "active_since": None,
        "last_sent_at": None,
        "last_value": None,
        "last_severity": None,
        "last_message": None,
        "notified": False,
        "pending": None,
        "delivery_error": None,
    }


def _pending_from_event(event: AlertEvent) -> dict[str, str]:
    return {
        "kind": event.kind,
        "severity": event.severity,
        "message": event.message,
    }


def _event_from_pending(key: str, pending: dict[str, Any]) -> AlertEvent:
    return AlertEvent(
        key=key,
        severity=str(pending["severity"]),
        message=str(pending["message"]),
        kind=str(pending["kind"]),
    )


def _percent(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, value * 100.0 / total))


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Monitoring clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value else None
