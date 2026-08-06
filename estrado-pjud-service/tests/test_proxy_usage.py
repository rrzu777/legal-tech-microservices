import re
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.bandwidth import record_proxy_request, record_proxy_response
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from worker.proxy_usage import ProxyUsageTracker


def _response(data):
    return MagicMock(data=data)


def _supabase():
    sb = MagicMock()
    chain = MagicMock()
    sb.rpc.return_value = chain
    sb.from_.return_value = chain
    chain.insert.return_value = chain
    return sb


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
    assert "402" not in str(payload)
    assert "secret-provider-detail" not in str(payload)


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
