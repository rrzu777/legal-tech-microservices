from __future__ import annotations

import asyncio
import json
import logging

import pytest

from app.ojv.errors import (
    InvalidCredentialsError,
    OjvTimeoutError,
    OjvUpstreamChangedError,
    OjvWafError,
    SessionExpiredError,
)
from app.ojv.private_telemetry import (
    PrivateOperationalMetrics,
    emit_private_event,
    sanitize_private_diagnostic,
    serialize_private_exception,
)


SECRETS = (
    "11.111.111-1",
    "persona@example.test",
    "synthetic-password-never-log",
    "OJVID=synthetic-cookie",
    "Bearer synthetic-token",
    "<html>PERSONA A / PERSONA B</html>",
    "PERSONA A / PERSONA B",
    "Abogada Litigante",
    "Se provee escrito reservado",
)


def assert_no_fixture_leaks(value: object) -> None:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    for secret in SECRETS:
        assert secret not in rendered


def test_recursive_diagnostics_drop_unknown_keys_and_redact_nested_values() -> None:
    diagnostic = sanitize_private_diagnostic(
        {
            "event": "private_resolution",
            "status": "failed",
            "error_code": "upstream_changed",
            "stage": "detail",
            "request": {
                "rut": SECRETS[0],
                "email": SECRETS[1],
                "password": SECRETS[2],
                "headers": {"cookie": SECRETS[3], "authorization": SECRETS[4]},
                "body": {"html": SECRETS[5], "caption": SECRETS[6]},
                "litigants": [SECRETS[7]],
                "movements": [SECRETS[8]],
            },
        }
    )
    assert diagnostic == {
        "event": "private_resolution",
        "status": "failed",
        "error_code": "upstream_changed",
        "stage": "detail",
    }
    assert_no_fixture_leaks(diagnostic)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (InvalidCredentialsError(*SECRETS), "credential_invalid"),
        (SessionExpiredError(*SECRETS), "session_expired"),
        (OjvWafError(*SECRETS), "waf"),
        (OjvTimeoutError(*SECRETS), "timeout"),
        (OjvUpstreamChangedError(*SECRETS), "upstream_changed"),
        (asyncio.CancelledError(*SECRETS), "cancelled"),
        (RuntimeError(" ".join(SECRETS)), "upstream_changed"),
    ],
)
def test_exception_serialization_is_closed_and_never_walks_arbitrary_payloads(
    error: BaseException,
    code: str,
) -> None:
    serialized = serialize_private_exception(error)
    assert serialized == {"error_code": code}
    assert_no_fixture_leaks(serialized)


def test_structured_and_rendered_private_logs_contain_only_allowlisted_fields(caplog) -> None:
    logger = logging.getLogger("test.private.telemetry")
    with caplog.at_level(logging.INFO, logger=logger.name):
        emit_private_event(
            logger,
            event="private_resolution",
            status="failed",
            error_code="waf",
            stage="login",
        )
        emit_private_event(
            logger,
            event="private_session",
            status="cancelled",
            error_code="cancelled",
            stage="shutdown",
        )

    assert [json.loads(record.message) for record in caplog.records] == [
        {
            "error_code": "waf",
            "event": "private_resolution",
            "stage": "login",
            "status": "failed",
        },
        {
            "error_code": "cancelled",
            "event": "private_session",
            "stage": "shutdown",
            "status": "cancelled",
        },
    ]
    assert_no_fixture_leaks(caplog.text)


def test_operational_snapshot_has_fixed_aggregate_dimensions_and_bounded_alerts() -> None:
    metrics = PrivateOperationalMetrics()
    for result in (
        "credential_invalid", "session_expired", "waf", "timeout",
        "upstream_changed", "lease_churn", "lease_loss",
        "retry_exhaustion", "incomplete_enrichment",
    ):
        metrics.record_attempt()
        metrics.record_result(result)

    snapshot = metrics.snapshot()
    assert snapshot == {
        "attempts": 9,
        "credential_invalid": 1,
        "session_expired": 1,
        "waf": 1,
        "timeout": 1,
        "upstream_schema_change": 1,
        "lease_churn": 1,
        "lease_loss": 1,
        "retry_exhaustion": 1,
        "incomplete_enrichment": 1,
    }
    assert_no_fixture_leaks(snapshot)
    with pytest.raises(ValueError, match="invalid_private_metric_code"):
        metrics.record_result("credential:11.111.111-1")


def test_health_schema_exposes_one_nested_aggregate_without_dimensions() -> None:
    from pydantic import ValidationError

    from app.models import HealthResponse, PrivateSyncHealth

    field = HealthResponse.model_fields["private_sync"]
    assert field.is_required() is False
    expected = {
        "attempts",
        "credential_invalid",
        "session_expired",
        "waf",
        "timeout",
        "upstream_schema_change",
        "lease_churn",
        "lease_loss",
        "retry_exhaustion",
        "incomplete_enrichment",
    }
    assert set(PrivateSyncHealth.model_fields) == expected
    with pytest.raises(ValidationError):
        PrivateSyncHealth.model_validate({"credential_id": 1})
