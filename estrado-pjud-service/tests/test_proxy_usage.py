import re
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pytest
from postgrest.exceptions import APIError

from app.bandwidth import record_proxy_request, record_proxy_response
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.failure_kind import MintUnavailableError
from worker.proxy_usage import ProxyUsageTracker
from app.usage_context import usage_scope


def _response(data):
    return MagicMock(data=data)


def _supabase():
    sb = MagicMock()
    chain = MagicMock()
    sb.rpc.return_value = chain
    sb.from_.return_value = chain
    chain.insert.return_value = chain
    chain.upsert.return_value = chain
    return sb


@pytest.mark.asyncio
async def test_usage_tracker_passes_allowlisted_lifecycle_failure_code():
    """Dropping the closed code would make a failed mint unclassifiable."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        async with tracker.track(
            operation="mint",
            transaction_key="mint-closed-failure",
            failure_code="mint_navigation_failed",
        ) as usage:
            record_proxy_request(10)
            usage.status = "error"
            usage.error_kind = "infra"

    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["failure_code"] == "mint_navigation_failed"


@pytest.mark.asyncio
async def test_usage_tracker_maps_mint_exception_without_raw_detail():
    """A typed mint failure must persist only its closed aggregate code."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        with pytest.raises(MintUnavailableError):
            async with tracker.track(
                operation="mint",
                transaction_key="mint-deadline-failure",
            ):
                record_proxy_request(10)
                raise MintUnavailableError("deadline_exceeded")

    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["failure_code"] == "mint_deadline_exceeded"
    assert "deadline_exceeded" not in str({
        key: value for key, value in payload.items() if key != "failure_code"
    })


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "failure_code"),
    [
        ("mint", "secret-provider-error"),
        ("search", "remote_protocol_disconnect"),
    ],
)
async def test_usage_tracker_rejects_open_or_non_lifecycle_failure_code_before_reservation(
    operation, failure_code,
):
    """Invalid taxonomy input must fail before any budget or provider traffic."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with pytest.raises(ValueError, match="failure_code"):
        async with tracker.track(
            operation=operation,
            failure_code=failure_code,
        ):
            pytest.fail("provider work must not start")

    sb.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_usage_tracker_reserves_persists_and_finalizes_actual_bytes():
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True, price_per_gb_usd=6.25)
    responses = [
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]

    with patch("worker.proxy_usage.run_query", side_effect=responses):
        async with tracker.track(
            operation="detail",
            law_firm_id="firm-1",
            case_id="case-1",
            sync_run_id="run-1",
            transaction_key="run-1:detail",
        ) as usage:
            record_proxy_request(125)
            record_proxy_response(875)
            usage.documents_downloaded = 1

    reserve = sb.rpc.call_args_list[0]
    assert reserve.args[0] == "pjud_proxy_reserve_budget"
    assert reserve.args[1]["p_operation"] == "detail"
    assert reserve.args[1]["p_case_id"] == "case-1"
    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["reservation_id"] == "reservation-1"
    assert payload["bytes_up"] == 125
    assert payload["bytes_down"] == 875
    assert payload["estimated_bytes_floor"] == 0
    assert payload["measurement_status"] == "measured"
    assert payload["request_count"] == 1
    assert payload["documents_downloaded"] == 1
    assert payload["status"] == "success"
    assert re.fullmatch(r"[a-f0-9]{64}", payload["idempotency_key"])
    finalize = sb.rpc.call_args_list[1]
    assert finalize.args[0] == "pjud_proxy_finalize_budget_reservation"
    assert finalize.args[1]["p_reservation_id"] == "reservation-1"
    assert finalize.args[1]["p_release"] is False


@pytest.mark.asyncio
async def test_session_usage_serializes_closed_safe_metadata_and_clamps_age():
    """Dropping lifecycle fields or persisting an unbounded age hides session cost."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    cycle = UUID("11111111-1111-4111-8111-111111111111")

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        async with tracker.track(
            operation="health",
            transaction_key="health-1",
            session_cycle_id=cycle,
            session_reason="soft_age",
            session_age_seconds=100_000,
        ):
            record_proxy_request(10)
            record_proxy_response(90)

    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["session_cycle_id"] == str(cycle)
    assert payload["session_reason"] == "soft_age"
    assert payload["session_age_seconds"] == 86_400
    assert "cookies" not in payload
    assert "proxy_url" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"session_cycle_id": UUID("11111111-1111-4111-8111-111111111111")},
        {"session_reason": "soft_age", "session_age_seconds": 1_201},
        {
            "session_cycle_id": UUID("11111111-1111-4111-8111-111111111111"),
            "session_reason": "soft_age",
        },
    ],
)
async def test_session_usage_rejects_incomplete_metadata_before_reservation(metadata):
    """A partial lifecycle tuple would make aggregate evidence permanently partial."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with pytest.raises(ValueError, match="session telemetry"):
        async with tracker.track(operation="health", **metadata):
            pass

    sb.rpc.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "reason", "age"),
    [
        ("search", "soft_age", 1_201),
        ("health", "free_form_reason", 1_201),
        ("health", "soft_age", -1),
        ("health", "soft_age", True),
    ],
)
async def test_session_usage_rejects_open_or_non_lifecycle_metadata(
    operation, reason, age,
):
    """Open reasons or metadata on business traffic would violate the SQL contract."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with pytest.raises(ValueError, match="session telemetry"):
        async with tracker.track(
            operation=operation,
            session_cycle_id=UUID("11111111-1111-4111-8111-111111111111"),
            session_reason=reason,
            session_age_seconds=age,
        ):
            pass

    sb.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_transient_reservation_disconnect_retries_before_provider_work():
    """Removing bounded retry would turn one lost HTTP/2 response into a global pause."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    entered = False

    with patch("worker.proxy_usage.run_query", side_effect=[
            httpx.RemoteProtocolError("server disconnected"),
            _response([]),
            _response([{
                "allowed": True,
                "reservation_id": "reservation-1",
                "claim_status": "claimed",
                "blocking_scope": None,
            }]),
            _response([{"id": "event-1"}]),
            _response(None),
        ]):
        async with tracker.track(
            operation="search",
            transaction_key="run-1:search",
        ):
            entered = True
            record_proxy_request(10)
            record_proxy_response(90)

    assert entered is True


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["40001", "40P01", "55P03"])
async def test_transient_reservation_database_contention_retries_before_provider_work(code):
    """A bounded PostgreSQL concurrency race during startup is not a ledger outage."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    entered = False
    serialization_failure = APIError({
        "message": "transient database contention",
        "code": code,
        "hint": None,
        "details": None,
    })

    with patch("worker.proxy_usage.run_query", side_effect=[
        serialization_failure,
        _response([]),
        _response([{
            "allowed": True,
            "reservation_id": "reservation-1",
            "claim_status": "claimed",
            "blocking_scope": None,
        }]),
        _response([{ "id": "event-1" }]),
        _response(None),
    ]):
        async with tracker.track(
            operation="mint",
            transaction_key="slot:0:attempt:1:test",
        ):
            entered = True
            record_proxy_request(10)
            record_proxy_response(90)

    assert entered is True
    reserve_calls = [
        call for call in sb.rpc.call_args_list
        if call.args[0] == "pjud_proxy_reserve_budget"
    ]
    assert len(reserve_calls) == 2


@pytest.mark.asyncio
async def test_transient_reservation_contention_during_reconciliation_retries_same_claim():
    """A contention-only reconciliation read cannot turn into a permanent pause."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    contention = APIError({
        "message": "could not serialize access due to concurrent update",
        "code": "40001",
        "hint": None,
        "details": None,
    })

    with patch("worker.proxy_usage.run_query", side_effect=[
        contention,
        contention,
        _response([{
            "allowed": True,
            "reservation_id": "reservation-1",
            "claim_status": "claimed",
            "blocking_scope": None,
        }]),
        _response([{ "id": "event-1" }]),
        _response(None),
    ]):
        async with tracker.track(
            operation="mint",
            transaction_key="slot:0:attempt:1:reconcile-contention",
        ):
            record_proxy_request(10)
            record_proxy_response(90)

    reserve_calls = [
        call for call in sb.rpc.call_args_list
        if call.args[0] == "pjud_proxy_reserve_budget"
    ]
    assert len(reserve_calls) == 2
    assert reserve_calls[0].args[1] == reserve_calls[1].args[1]


@pytest.mark.asyncio
async def test_transient_reservation_contention_exhausts_without_provider_work():
    """Persistent DB contention remains fail-closed after the bounded retries."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    contention = APIError({
        "message": "could not serialize access due to concurrent update",
        "code": "40001",
        "hint": None,
        "details": None,
    })

    with patch("worker.proxy_usage.run_query", side_effect=[contention] * 6):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="mint",
                transaction_key="slot:0:attempt:1:persistent-contention",
            ):
                pytest.fail("provider work must not start")

    reserve_calls = [
        call for call in sb.rpc.call_args_list
        if call.args[0] == "pjud_proxy_reserve_budget"
    ]
    assert len(reserve_calls) == 3
    assert len({str(call.args[1]) for call in reserve_calls}) == 1


@pytest.mark.asyncio
async def test_reservation_reconciliation_constraint_is_not_retried_and_logs_safe_code(caplog):
    """A deterministic reconciliation failure stays fail-closed but diagnosable."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    contention = APIError({
        "message": "could not serialize access due to concurrent update",
        "code": "40001",
        "hint": None,
        "details": None,
    })
    constraint = APIError({
        "message": "reservation ownership rejected",
        "code": "23514",
        "hint": None,
        "details": None,
    })

    with patch("worker.proxy_usage.run_query", side_effect=[contention, constraint]):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="mint",
                transaction_key="slot:0:attempt:1:reconcile-constraint",
            ):
                pytest.fail("provider work must not start")

    reserve_calls = [
        call for call in sb.rpc.call_args_list
        if call.args[0] == "pjud_proxy_reserve_budget"
    ]
    assert len(reserve_calls) == 1
    assert "reconciliation rejected operation=mint code=23514; not retrying" in caplog.text
    assert "reservation ownership rejected" not in caplog.text


@pytest.mark.asyncio
async def test_reservation_constraint_error_is_not_retried(caplog):
    """Only transient database concurrency errors may retry before proxy traffic."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    constraint = APIError({
        "message": "invalid budget reservation",
        "code": "23514",
        "hint": None,
        "details": None,
    })

    with patch("worker.proxy_usage.run_query", side_effect=constraint):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="mint",
                transaction_key="slot:0:attempt:1:test",
            ):
                pytest.fail("provider work must not start")

    reserve_calls = [
        call for call in sb.rpc.call_args_list
        if call.args[0] == "pjud_proxy_reserve_budget"
    ]
    assert len(reserve_calls) == 1
    assert "code=23514; not retrying" in caplog.text
    assert "invalid budget reservation" not in caplog.text


@pytest.mark.asyncio
async def test_ambiguous_reservation_response_recovers_same_claim_without_second_reserve():
    """A committed reserve with a lost response must be recovered, not charged twice."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    entered = False
    claim_token = UUID("11111111-1111-4111-8111-111111111111")
    request_id = UUID("22222222-2222-4222-8222-222222222222")

    with (
        patch("worker.proxy_usage.uuid.uuid4", side_effect=[claim_token, request_id]),
        patch("worker.proxy_usage.run_query", side_effect=[
            httpx.RemoteProtocolError("server disconnected"),
            _response([{
                "id": "reservation-1",
                "claim_token": str(claim_token),
                "status": "reserved",
                "blocking_scope": None,
            }]),
            _response([{"id": "event-1"}]),
            _response(None),
        ]),
    ):
        async with tracker.track(
            operation="detail",
            transaction_key="run-1:detail",
        ):
            entered = True
            record_proxy_request(10)
            record_proxy_response(90)

    assert entered is True
    assert sb.rpc.call_args_list[0].args[0] == "pjud_proxy_reserve_budget"
    assert len([
        call for call in sb.rpc.call_args_list
        if call.args[0] == "pjud_proxy_reserve_budget"
    ]) == 1


@pytest.mark.asyncio
async def test_reservation_retry_recovers_commit_that_appears_after_first_read():
    """A late commit must be adopted only after the repeated RPC proves its identity."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    claim_token = UUID("11111111-1111-4111-8111-111111111111")
    request_id = UUID("22222222-2222-4222-8222-222222222222")
    entered = False

    with (
        patch("worker.proxy_usage.uuid.uuid4", side_effect=[claim_token, request_id]),
        patch("worker.proxy_usage.run_query", side_effect=[
            httpx.RemoteProtocolError("server disconnected"),
            _response([]),
            _response([{
                "allowed": False,
                "reservation_id": "reservation-1",
                "claim_status": "already_reserved",
                "blocking_scope": None,
            }]),
            _response([{
                "id": "reservation-1",
                "claim_token": str(claim_token),
                "status": "reserved",
                "blocking_scope": None,
            }]),
            _response([{"id": "event-1"}]),
            _response(None),
        ]),
    ):
        async with tracker.track(
            operation="search",
            transaction_key="run-1:search",
        ):
            entered = True
            record_proxy_request(10)
            record_proxy_response(90)

    assert entered is True


@pytest.mark.asyncio
async def test_existing_reservation_with_foreign_claim_is_never_adopted():
    """A PostgREST filtering regression must not let one worker adopt another claim."""
    tracker = ProxyUsageTracker(_supabase(), enabled=True)
    entered = False

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": False,
            "reservation_id": "reservation-1",
            "claim_status": "already_reserved",
            "blocking_scope": None,
        }]),
        _response([{
            "id": "reservation-1",
            "claim_token": "foreign-claim",
            "status": "reserved",
            "blocking_scope": None,
        }]),
    ]):
        with pytest.raises(ProxyBudgetExceededError):
            async with tracker.track(
                operation="search",
                transaction_key="run-1:search",
            ):
                entered = True

    assert entered is False


@pytest.mark.asyncio
async def test_catalog_refresh_reserves_two_mb():
    """An underestimated refresh could bypass the intended cost guard."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True, price_per_gb_usd=6.25)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        async with tracker.track(operation="opportunistic_catalog_refresh"):
            record_proxy_request(10)
            record_proxy_response(90)

    reserve = sb.rpc.call_args_list[0]
    assert reserve.args[1]["p_estimated_cost_usd"] == pytest.approx(0.0125)


@pytest.mark.asyncio
async def test_mint_persists_typed_catalog_cause():
    """Dropping either causal field makes an indirect mint indistinguishable."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    session_id = UUID("22222222-2222-4222-8222-222222222222")

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        async with tracker.track(
            operation="mint",
            cause_operation="opportunistic_catalog_refresh",
            cause_session_id=session_id,
        ):
            record_proxy_request(10)
            record_proxy_response(90)

    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["operation"] == "mint"
    assert payload["cause_operation"] == "opportunistic_catalog_refresh"
    assert payload["cause_session_id"] == str(session_id)


@pytest.mark.asyncio
async def test_mint_persists_cause_attached_to_usage_after_tracker_enter():
    """Pool attribution is decided after reservation, not in track arguments."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    session_id = UUID("22222222-2222-4222-8222-222222222222")

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        async with tracker.track(operation="mint") as usage:
            usage.cause_operation = "opportunistic_catalog_refresh"
            usage.cause_session_id = session_id
            record_proxy_request(10)
            record_proxy_response(90)

    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["cause_operation"] == "opportunistic_catalog_refresh"
    assert payload["cause_session_id"] == str(session_id)
    assert usage.causal_event_persisted is True


@pytest.mark.asyncio
async def test_causal_insert_survives_finalize_failure_without_becoming_retryable():
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    session_id = UUID("22222222-2222-4222-8222-222222222222")
    usage = None

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        RuntimeError("finalize unavailable"),
    ]):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(operation="mint") as usage:
                usage.cause_operation = "opportunistic_catalog_refresh"
                usage.cause_session_id = session_id
                record_proxy_request(10)

    assert usage is not None
    assert usage.causal_event_persisted is True


@pytest.mark.asyncio
async def test_ambiguous_ledger_insert_accepts_only_the_persisted_immutable_event():
    """A lost insert response must reconcile the exact event before finalization."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    claim_token = UUID("11111111-1111-4111-8111-111111111111")
    request_id = UUID("22222222-2222-4222-8222-222222222222")
    persisted_event = {
        "idempotency_key": "ba434a67ea0be825573fbe923bb0147ad918d7f459bf843aa12097d1ea6438e0",
        "reservation_id": "reservation-1",
        "law_firm_id": None,
        "case_id": None,
        "sync_run_id": None,
        "movement_id": None,
        "cause_operation": None,
        "cause_session_id": None,
        "request_id": str(request_id),
        "component": "worker",
        "operation": "search",
        "bytes_up": 10,
        "bytes_down": 90,
        "estimated_bytes_floor": 0,
        "measurement_status": "measured",
        "request_count": 1,
        "retry_count": 0,
        "documents_downloaded": 0,
        "documents_skipped": 0,
        "status": "success",
        "error_kind": None,
        "provider": "iproyal",
        "price_per_gb_usd": 6.25,
    }

    with (
        patch("worker.proxy_usage.uuid.uuid4", side_effect=[claim_token, request_id]),
        patch("worker.proxy_usage.run_query", side_effect=[
            _response([{
                "allowed": True,
                "reservation_id": "reservation-1",
                "claim_status": "claimed",
                "blocking_scope": None,
            }]),
            httpx.RemoteProtocolError("server disconnected"),
            _response([persisted_event]),
            _response(None),
        ]),
    ):
        async with tracker.track(
            operation="search",
            transaction_key="run-1:search",
        ):
            record_proxy_request(10)
            record_proxy_response(90)

    assert sb.rpc.call_args_list[-1].args[0] == "pjud_proxy_finalize_budget_reservation"


@pytest.mark.asyncio
async def test_missing_ledger_after_lost_response_retries_with_ignore_duplicates(caplog):
    """A confirmed missing event should retry safely instead of opening the circuit."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True,
            "reservation_id": "reservation-1",
            "claim_status": "claimed",
            "blocking_scope": None,
        }]),
        httpx.RemoteProtocolError("server disconnected"),
        _response([]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        async with tracker.track(
            operation="search",
            transaction_key="run-1:search",
        ):
            record_proxy_request(10)
            record_proxy_response(90)

    retry = sb.from_.return_value.upsert.call_args
    assert retry.kwargs == {
        "on_conflict": "idempotency_key",
        "ignore_duplicates": True,
    }
    assert "boundary=ledger operation=search attempt=1/3" in caplog.text


@pytest.mark.asyncio
async def test_transient_ledger_reconciliation_read_retries_the_safe_insert():
    """A second transient read failure must remain inside the bounded recovery."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True,
            "reservation_id": "reservation-1",
            "claim_status": "claimed",
            "blocking_scope": None,
        }]),
        httpx.RemoteProtocolError("insert response lost"),
        httpx.RemoteProtocolError("reconciliation read disconnected"),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        async with tracker.track(
            operation="detail",
            transaction_key="run-1:detail",
        ):
            record_proxy_request(10)
            record_proxy_response(90)


@pytest.mark.asyncio
async def test_ledger_recovery_exhausts_exactly_three_write_attempts():
    """Persistent ambiguity must fail closed after three writes, never loop forever."""
    tracker = ProxyUsageTracker(_supabase(), enabled=True)
    calls = [
        _response([{
            "allowed": True,
            "reservation_id": "reservation-1",
            "claim_status": "claimed",
            "blocking_scope": None,
        }]),
    ]
    for _ in range(3):
        calls.extend([
            httpx.RemoteProtocolError("server disconnected"),
            _response([]),
        ])

    with patch("worker.proxy_usage.run_query", side_effect=calls) as query:
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="search",
                transaction_key="run-1:search",
            ):
                record_proxy_request(10)
                record_proxy_response(90)

    assert query.await_count == 7


@pytest.mark.asyncio
async def test_ambiguous_ledger_rejects_any_immutable_field_mismatch():
    """A colliding event with different measured bytes must never be adopted."""
    tracker = ProxyUsageTracker(_supabase(), enabled=True)
    claim_token = UUID("11111111-1111-4111-8111-111111111111")
    request_id = UUID("22222222-2222-4222-8222-222222222222")
    mismatched_event = {
        "idempotency_key": "ba434a67ea0be825573fbe923bb0147ad918d7f459bf843aa12097d1ea6438e0",
        "reservation_id": "reservation-1",
        "law_firm_id": None,
        "case_id": None,
        "sync_run_id": None,
        "movement_id": None,
        "cause_operation": None,
        "cause_session_id": None,
        "request_id": str(request_id),
        "component": "worker",
        "operation": "search",
        "bytes_up": 10,
        "bytes_down": 91,
        "estimated_bytes_floor": 0,
        "measurement_status": "measured",
        "request_count": 1,
        "retry_count": 0,
        "documents_downloaded": 0,
        "documents_skipped": 0,
        "status": "success",
        "error_kind": None,
        "provider": "iproyal",
        "price_per_gb_usd": 6.25,
    }

    with (
        patch("worker.proxy_usage.uuid.uuid4", side_effect=[claim_token, request_id]),
        patch("worker.proxy_usage.run_query", side_effect=[
            _response([{
                "allowed": True,
                "reservation_id": "reservation-1",
                "claim_status": "claimed",
                "blocking_scope": None,
            }]),
            httpx.RemoteProtocolError("server disconnected"),
            _response([mismatched_event]),
        ]),
    ):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="search",
                transaction_key="run-1:search",
            ):
                record_proxy_request(10)
                record_proxy_response(90)


@pytest.mark.asyncio
async def test_ambiguous_finalize_accepts_matching_terminal_reservation():
    """A committed finalization with a lost response must not pause telemetry."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    claim_token = UUID("11111111-1111-4111-8111-111111111111")
    request_id = UUID("22222222-2222-4222-8222-222222222222")

    with (
        patch("worker.proxy_usage.uuid.uuid4", side_effect=[claim_token, request_id]),
        patch("worker.proxy_usage.run_query", side_effect=[
            _response([{
                "allowed": True,
                "reservation_id": "reservation-1",
                "claim_status": "claimed",
                "blocking_scope": None,
            }]),
            _response([{"id": "event-1"}]),
            httpx.RemoteProtocolError("server disconnected"),
            _response([{
                "id": "reservation-1",
                "claim_token": str(claim_token),
                "status": "finalized",
            }]),
        ]),
    ):
        async with tracker.track(
            operation="detail",
            transaction_key="run-1:detail",
        ):
            record_proxy_request(10)
            record_proxy_response(90)


@pytest.mark.asyncio
async def test_finalize_retry_reconciles_already_resolved_race():
    """A late first commit may make the retry return 23514; durable state decides."""
    tracker = ProxyUsageTracker(_supabase(), enabled=True)
    claim_token = UUID("11111111-1111-4111-8111-111111111111")
    request_id = UUID("22222222-2222-4222-8222-222222222222")
    already_resolved = APIError({
        "message": "reservation is already resolved",
        "code": "23514",
        "hint": None,
        "details": None,
    })

    with (
        patch("worker.proxy_usage.uuid.uuid4", side_effect=[claim_token, request_id]),
        patch("worker.proxy_usage.run_query", side_effect=[
            _response([{
                "allowed": True,
                "reservation_id": "reservation-1",
                "claim_status": "claimed",
                "blocking_scope": None,
            }]),
            _response([{"id": "event-1"}]),
            httpx.RemoteProtocolError("server disconnected"),
            _response([{
                "id": "reservation-1",
                "claim_token": str(claim_token),
                "status": "reserved",
            }]),
            already_resolved,
            _response([{
                "id": "reservation-1",
                "claim_token": str(claim_token),
                "status": "finalized",
            }]),
        ]),
    ):
        async with tracker.track(
            operation="detail",
            transaction_key="run-1:detail",
        ):
            record_proxy_request(10)
            record_proxy_response(90)


@pytest.mark.asyncio
async def test_finalize_constraint_error_is_reconciled_once_and_not_retried():
    """A deterministic 23514 must never be mislabeled as a transient retry."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)
    constraint = APIError({
        "message": "reservation with usage cannot be released",
        "code": "23514",
        "hint": None,
        "details": None,
    })
    reserved = _response([{
        "id": "reservation-1",
        "claim_token": "11111111-1111-4111-8111-111111111111",
        "status": "reserved",
    }])

    with (
        patch("worker.proxy_usage.uuid.uuid4", return_value=UUID(
            "11111111-1111-4111-8111-111111111111"
        )),
        patch("worker.proxy_usage.run_query", side_effect=[
            _response([{
                "allowed": True,
                "reservation_id": "reservation-1",
                "claim_status": "claimed",
                "blocking_scope": None,
            }]),
            constraint,
            reserved,
            constraint,
            reserved,
            constraint,
            reserved,
        ]),
    ):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="health",
                transaction_key="health-1",
            ):
                pass

    finalize_calls = [
        call for call in sb.rpc.call_args_list
        if call.args[0] == "pjud_proxy_finalize_budget_reservation"
    ]
    assert len(finalize_calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_release_retries_when_reservation_is_still_reserved():
    """A release that provably did not commit is safe to retry with the same claim."""
    tracker = ProxyUsageTracker(_supabase(), enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True,
            "reservation_id": "reservation-1",
            "claim_status": "claimed",
            "blocking_scope": None,
        }]),
        httpx.RemoteProtocolError("server disconnected"),
        _response([{
            "id": "reservation-1",
            "claim_token": "11111111-1111-4111-8111-111111111111",
            "status": "reserved",
        }]),
        _response(None),
    ]):
        with patch("worker.proxy_usage.uuid.uuid4", return_value=UUID(
            "11111111-1111-4111-8111-111111111111"
        )):
            async with tracker.track(
                operation="health",
                transaction_key="health-1",
            ):
                pass


@pytest.mark.asyncio
async def test_ambiguous_release_rejects_the_opposite_terminal_state():
    """A finalized paid reservation cannot be reinterpreted as a release."""
    tracker = ProxyUsageTracker(_supabase(), enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True,
            "reservation_id": "reservation-1",
            "claim_status": "claimed",
            "blocking_scope": None,
        }]),
        httpx.RemoteProtocolError("server disconnected"),
        _response([{
            "id": "reservation-1",
            "claim_token": "11111111-1111-4111-8111-111111111111",
            "status": "finalized",
        }]),
    ]):
        with patch("worker.proxy_usage.uuid.uuid4", return_value=UUID(
            "11111111-1111-4111-8111-111111111111"
        )):
            with pytest.raises(ProxyUsagePersistenceError):
                async with tracker.track(
                    operation="health",
                    transaction_key="health-1",
                ):
                    pass


@pytest.mark.asyncio
async def test_finalize_recovery_exhausts_exactly_three_attempts():
    """Persistent finalize ambiguity remains fail-closed and bounded."""
    tracker = ProxyUsageTracker(_supabase(), enabled=True)
    calls = [_response([{
        "allowed": True,
        "reservation_id": "reservation-1",
        "claim_status": "claimed",
        "blocking_scope": None,
    }])]
    for _ in range(3):
        calls.extend([
            httpx.RemoteProtocolError("server disconnected"),
            _response([{
                "id": "reservation-1",
                "claim_token": "11111111-1111-4111-8111-111111111111",
                "status": "reserved",
            }]),
        ])

    with (
        patch("worker.proxy_usage.uuid.uuid4", return_value=UUID(
            "11111111-1111-4111-8111-111111111111"
        )),
        patch("worker.proxy_usage.run_query", side_effect=calls) as query,
    ):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="health",
                transaction_key="health-1",
            ):
                pass

    assert query.await_count == 7


@pytest.mark.asyncio
async def test_usage_tracker_inherits_request_attribution_for_mint_search_and_detail():
    """Dropping request context must make paid API work unattributed again."""
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True, component="api")
    responses = []
    for index in range(3):
        responses.extend([
            _response([{"id": "33333333-3333-4333-8333-333333333333"}]),
            _response([{
                "allowed": True,
                "reservation_id": f"reservation-{index}",
                "claim_status": "claimed",
                "blocking_scope": None,
            }]),
            _response([{"id": f"event-{index}"}]),
            _response(None),
        ])

    with patch("worker.proxy_usage.run_query", side_effect=responses):
        with usage_scope(
            law_firm_id="11111111-1111-4111-8111-111111111111",
            case_id="22222222-2222-4222-8222-222222222222",
            sync_run_id="33333333-3333-4333-8333-333333333333",
        ):
            for operation in ("mint", "search", "detail"):
                async with tracker.track(operation=operation):
                    record_proxy_request(10)
                    record_proxy_response(90)

    payloads = [call.args[0] for call in sb.from_.return_value.insert.call_args_list]
    assert [payload["operation"] for payload in payloads] == ["mint", "search", "detail"]
    assert all(payload["law_firm_id"] == "11111111-1111-4111-8111-111111111111" for payload in payloads)
    assert all(payload["case_id"] == "22222222-2222-4222-8222-222222222222" for payload in payloads)
    assert all(payload["sync_run_id"] == "33333333-3333-4333-8333-333333333333" for payload in payloads)


@pytest.mark.asyncio
async def test_api_rejects_unknown_sync_run_before_reservation_or_provider_work():
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True, component="api")
    entered = False

    with patch("worker.proxy_usage.run_query", return_value=_response([])):
        with pytest.raises(ProxyUsagePersistenceError):
            with usage_scope(
                law_firm_id="11111111-1111-4111-8111-111111111111",
                case_id="22222222-2222-4222-8222-222222222222",
                sync_run_id="33333333-3333-4333-8333-333333333333",
            ):
                async with tracker.track(operation="search"):
                    entered = True

    assert entered is False
    sb.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_api_transient_scope_read_retries_before_reservation():
    """A transient attribution read must not mint or pause until bounded retry ends."""
    tracker = ProxyUsageTracker(_supabase(), enabled=True, component="api")
    entered = False

    with patch("worker.proxy_usage.run_query", side_effect=[
        httpx.RemoteProtocolError("server disconnected"),
        _response([{"id": "33333333-3333-4333-8333-333333333333"}]),
        _response([{
            "allowed": True,
            "reservation_id": "reservation-1",
            "claim_status": "claimed",
            "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        with usage_scope(
            law_firm_id="11111111-1111-4111-8111-111111111111",
            case_id="22222222-2222-4222-8222-222222222222",
            sync_run_id="33333333-3333-4333-8333-333333333333",
        ):
            async with tracker.track(
                operation="search",
                transaction_key="run-1:search",
            ):
                entered = True
                record_proxy_request(10)
                record_proxy_response(90)

    assert entered is True


@pytest.mark.asyncio
async def test_zero_request_skip_releases_reservation_but_keeps_savings_event():
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        async with tracker.track(
            operation="document_primary",
            law_firm_id="firm-1",
            case_id="case-1",
            transaction_key="run-1:doc:1",
        ) as usage:
            usage.documents_skipped = 1

    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["reservation_id"] is None
    assert payload["status"] == "skipped"
    assert payload["documents_skipped"] == 1
    finalize = sb.rpc.call_args_list[1]
    assert finalize.args[1]["p_release"] is True


@pytest.mark.asyncio
async def test_budget_denial_stops_before_provider_context_is_entered():
    tracker = ProxyUsageTracker(_supabase(), enabled=True)
    entered = False

    with patch("worker.proxy_usage.run_query", return_value=_response([{
        "allowed": False, "reservation_id": "reservation-1",
        "claim_status": "blocked", "blocking_scope": "case",
    }])):
        with pytest.raises(ProxyBudgetExceededError) as denied:
            async with tracker.track(
                operation="search", law_firm_id="firm-1", case_id="case-1",
            ):
                entered = True

    assert entered is False
    assert denied.value.blocking_scope == "case"


@pytest.mark.asyncio
async def test_persistence_failure_replaces_provider_error_and_fails_closed():
    tracker = ProxyUsageTracker(_supabase(), enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        RuntimeError("database unavailable"),
    ]):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(operation="search"):
                record_proxy_request(10)
                raise httpx.ReadError("provider also failed")


@pytest.mark.asyncio
async def test_unmeasurable_response_consumes_conservative_reserved_floor():
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        with pytest.raises(httpx.ReadError):
            async with tracker.track(operation="mint"):
                record_proxy_request(0)
                raise httpx.ReadError("response size unavailable")

    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["bytes_up"] == 0
    assert payload["bytes_down"] == 0
    assert payload["estimated_bytes_floor"] == 10_000_000
    assert payload["measurement_status"] == "estimated_floor"
    finalize = sb.rpc.call_args_list[1]
    assert finalize.args[1]["p_release"] is False


@pytest.mark.asyncio
async def test_billing_error_is_recorded_for_ops_without_raw_message():
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        with pytest.raises(httpx.ProxyError):
            async with tracker.track(
                operation="search", law_firm_id="firm-1", case_id="case-1",
            ):
                record_proxy_request(10)
                raise httpx.ProxyError("402 Payment Required secret-provider-detail")

    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["status"] == "error"
    assert payload["error_kind"] == "billing"
    assert "error_message" not in payload
    assert "secret-provider-detail" not in str(payload.values())


@pytest.mark.asyncio
async def test_upstream_block_can_be_classified_without_changing_transport_counts():
    sb = _supabase()
    tracker = ProxyUsageTracker(sb, enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response([{
            "allowed": True, "reservation_id": "reservation-1",
            "claim_status": "claimed", "blocking_scope": None,
        }]),
        _response([{"id": "event-1"}]),
        _response(None),
    ]):
        async with tracker.track(operation="detail") as usage:
            record_proxy_request(10)
            record_proxy_response(90)
            usage.status = "blocked"
            usage.error_kind = "ojv"

    payload = sb.from_.return_value.insert.call_args.args[0]
    assert payload["status"] == "blocked"
    assert payload["error_kind"] == "ojv"
