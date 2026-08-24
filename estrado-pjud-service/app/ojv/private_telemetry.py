"""Closed telemetry boundary for authenticated private OJV work.

No API in this module accepts identifiers, upstream text or arbitrary labels.
That is intentional: aggregation and redaction are enforced by construction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Mapping
from typing import Literal

from app.ojv.errors import OjvSessionError


PrivateEvent = Literal["private_resolution", "private_session"]
PrivateStatus = Literal["ok", "failed", "cancelled", "closed"]
PrivateStage = Literal["login", "detail", "movements", "commit", "shutdown"]
PrivateErrorCode = Literal[
    "credential_invalid",
    "session_expired",
    "waf",
    "timeout",
    "upstream_changed",
    "private_fence_unavailable",
    "cancelled",
]
PrivateMetricCode = Literal[
    "credential_invalid",
    "session_expired",
    "waf",
    "timeout",
    "upstream_changed",
    "lease_churn",
    "lease_loss",
    "retry_exhaustion",
    "incomplete_enrichment",
]

_EVENTS = frozenset(("private_resolution", "private_session"))
_STATUSES = frozenset(("ok", "failed", "cancelled", "closed"))
_STAGES = frozenset(("login", "detail", "movements", "commit", "shutdown"))
_ERROR_CODES = frozenset((
    "credential_invalid", "session_expired", "waf", "timeout",
    "upstream_changed", "private_fence_unavailable", "cancelled",
))
_METRIC_CODES = frozenset((
    "credential_invalid", "session_expired", "waf", "timeout",
    "upstream_changed", "lease_churn", "lease_loss", "retry_exhaustion",
    "incomplete_enrichment",
))
_DIAGNOSTIC_KEYS = frozenset(("event", "status", "error_code", "stage"))


def sanitize_private_diagnostic(value: object) -> dict[str, str]:
    """Recursively fail closed by retaining only the finite leaf contract.

    Nested data is never operationally useful here and may contain RUTs,
    parties, movements, headers or raw upstream bodies, so it is dropped.
    """
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, str] = {}
    for key in _DIAGNOSTIC_KEYS:
        item = value.get(key)
        if not isinstance(item, str):
            continue
        if key == "event" and item in _EVENTS:
            safe[key] = item
        elif key == "status" and item in _STATUSES:
            safe[key] = item
        elif key == "error_code" and item in _ERROR_CODES:
            safe[key] = item
        elif key == "stage" and item in _STAGES:
            safe[key] = item
    return safe


def serialize_private_exception(error: BaseException) -> dict[str, str]:
    if isinstance(error, OjvSessionError):
        code = error.code.value
    elif isinstance(error, asyncio.CancelledError):
        code = "cancelled"
    elif isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        code = "timeout"
    else:
        code = "upstream_changed"
    return {"error_code": code}


def emit_private_event(
    logger: logging.Logger,
    *,
    event: PrivateEvent,
    status: PrivateStatus,
    error_code: PrivateErrorCode | None = None,
    stage: PrivateStage | None = None,
) -> None:
    payload = sanitize_private_diagnostic({
        "event": event,
        "status": status,
        "error_code": error_code,
        "stage": stage,
    })
    # sort_keys makes production-like rendered logs deterministic for alerting.
    logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


class PrivateOperationalMetrics:
    """Process-local, finite counters; no label/dimension API exists."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts = 0
        self._counts = {code: 0 for code in _METRIC_CODES}

    def record_attempt(self) -> None:
        with self._lock:
            self._attempts += 1

    def record_result(self, code: PrivateMetricCode) -> None:
        if code not in _METRIC_CODES:
            raise ValueError("invalid_private_metric_code")
        with self._lock:
            self._counts[code] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "attempts": self._attempts,
                "credential_invalid": self._counts["credential_invalid"],
                "session_expired": self._counts["session_expired"],
                "waf": self._counts["waf"],
                "timeout": self._counts["timeout"],
                "upstream_schema_change": self._counts["upstream_changed"],
                "lease_churn": self._counts["lease_churn"],
                "lease_loss": self._counts["lease_loss"],
                "retry_exhaustion": self._counts["retry_exhaustion"],
                "incomplete_enrichment": self._counts["incomplete_enrichment"],
            }


private_operational_metrics = PrivateOperationalMetrics()
