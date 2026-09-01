from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr

from app.bandwidth import record_proxy_request, record_proxy_response
from app.proxy_cost import ProxyUsagePersistenceError
from worker.proxy_usage import ProxyUsageTracker
from worker.trial_scope import TrialScope


def _response(data):
    return MagicMock(data=data)


def _supabase():
    sb = MagicMock()
    chain = MagicMock()
    sb.rpc.return_value = chain
    sb.from_.return_value = chain
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.limit.return_value = chain
    return sb


def _scope() -> TrialScope:
    return TrialScope(
        capability=SecretStr("a" * 64),
        runtime_generation="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        trial_grant_id="11111111-1111-4111-8111-111111111111",
        job_id="22222222-2222-4222-8222-222222222222",
        claim_token="33333333-3333-4333-8333-333333333333",
        worker_id="import-worker",
        law_firm_id="44444444-4444-4444-8444-444444444444",
        credential_id="55555555-5555-4555-8555-555555555555",
        expected_credentials_updated_at=datetime(
            2026, 9, 1, 12, 0, tzinfo=timezone.utc,
        ),
    )


def _attribution(scope: TrialScope) -> dict:
    return {
        "law_firm_id": str(scope.law_firm_id),
        "import_job_id": str(scope.job_id),
        "import_claim_token": str(scope.claim_token),
        "import_worker_id": scope.worker_id,
    }


@pytest.mark.asyncio
async def test_trial_usage_uses_only_dedicated_rpcs_with_exact_bound_tuple():
    normal = _supabase()
    trial = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(
        normal,
        trial_supabase=trial,
        enabled=True,
        price_per_gb_usd=6.25,
    )

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response({
            "allowed": True,
            "reservation_id": "66666666-6666-4666-8666-666666666666",
            "claim_status": "claimed",
            "blocking_scope": None,
        }),
        _response("77777777-7777-4777-8777-777777777777"),
        _response(True),
    ]):
        async with tracker.track(
            operation="search",
            transaction_key="trial:page:civil:1",
            trial_scope=scope,
            **_attribution(scope),
        ) as usage:
            record_proxy_request(125)
            record_proxy_response(875)
            usage.retry_count = 1

    assert normal.rpc.call_args_list == []
    assert normal.from_.call_args_list == []
    assert trial.from_.call_args_list == []
    assert [call.args[0] for call in trial.rpc.call_args_list] == [
        "pjud_proxy_reserve_trial_budget",
        "pjud_proxy_record_trial_usage",
        "pjud_proxy_finalize_trial_budget_reservation",
    ]

    reserve = trial.rpc.call_args_list[0].args[1]
    assert set(reserve) == {
        "p_expected_generation",
        "p_trial_grant_id",
        "p_job_id",
        "p_import_claim_token",
        "p_worker_id",
        "p_estimated_cost_usd",
        "p_idempotency_key",
        "p_reservation_claim_token",
        "p_provider",
        "p_operation",
        "p_price_per_gb_usd",
    }
    assert reserve["p_expected_generation"] == str(scope.runtime_generation)
    assert reserve["p_trial_grant_id"] == str(scope.trial_grant_id)
    assert reserve["p_job_id"] == str(scope.job_id)
    assert reserve["p_import_claim_token"] == str(scope.claim_token)
    assert reserve["p_worker_id"] == scope.worker_id
    assert reserve["p_provider"] == "iproyal"
    assert reserve["p_operation"] == "search"
    assert reserve["p_price_per_gb_usd"] == 6.25

    record = trial.rpc.call_args_list[1].args[1]
    assert set(record) == {
        "p_expected_generation",
        "p_trial_grant_id",
        "p_job_id",
        "p_import_claim_token",
        "p_worker_id",
        "p_reservation_id",
        "p_reservation_claim_token",
        "p_payload",
    }
    assert record["p_reservation_claim_token"] == reserve["p_reservation_claim_token"]
    assert set(record["p_payload"]) == {
        "idempotency_key",
        "reservation_id",
        "law_firm_id",
        "case_id",
        "sync_run_id",
        "movement_id",
        "cause_operation",
        "cause_session_id",
        "request_id",
        "component",
        "operation",
        "bytes_up",
        "bytes_down",
        "estimated_bytes_floor",
        "measurement_status",
        "request_count",
        "retry_count",
        "documents_downloaded",
        "documents_skipped",
        "status",
        "error_kind",
        "failure_code",
        "provider",
        "price_per_gb_usd",
        "import_job_id",
        "trial_grant_id",
    }
    assert record["p_payload"]["trial_grant_id"] == str(scope.trial_grant_id)
    assert record["p_payload"]["import_job_id"] == str(scope.job_id)
    assert record["p_payload"]["bytes_up"] == 125
    assert record["p_payload"]["bytes_down"] == 875
    assert record["p_payload"]["law_firm_id"] == str(scope.law_firm_id)
    assert record["p_payload"]["case_id"] is None
    assert record["p_payload"]["sync_run_id"] is None
    assert record["p_payload"]["movement_id"] is None
    assert "capability" not in repr(record).lower()
    assert scope.capability.get_secret_value() not in repr(record)

    finalize = trial.rpc.call_args_list[2].args[1]
    assert set(finalize) == {
        "p_expected_generation",
        "p_trial_grant_id",
        "p_job_id",
        "p_import_claim_token",
        "p_worker_id",
        "p_reservation_id",
        "p_reservation_claim_token",
        "p_release",
    }
    assert finalize["p_release"] is False
    assert finalize["p_reservation_claim_token"] == reserve["p_reservation_claim_token"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("law_firm_id", None),
        ("law_firm_id", "99999999-9999-4999-8999-999999999999"),
        ("import_job_id", None),
        ("import_claim_token", "99999999-9999-4999-8999-999999999999"),
        ("import_worker_id", "other-worker"),
        ("movement_id", "99999999-9999-4999-8999-999999999999"),
    ],
)
async def test_trial_usage_wrong_or_missing_tuple_fails_before_reserve_or_provider(
    field, value,
):
    normal = _supabase()
    trial = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(normal, trial_supabase=trial, enabled=True)
    attribution = _attribution(scope)
    attribution[field] = value
    entered = False

    with pytest.raises(ProxyUsagePersistenceError):
        async with tracker.track(
            operation="search",
            transaction_key="wrong-tuple",
            trial_scope=scope,
            **attribution,
        ):
            entered = True

    assert entered is False
    normal.rpc.assert_not_called()
    normal.from_.assert_not_called()
    trial.rpc.assert_not_called()
    trial.from_.assert_not_called()


@pytest.mark.asyncio
async def test_trial_usage_never_falls_back_when_dedicated_client_is_missing():
    normal = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(normal, trial_supabase=None, enabled=True)

    with pytest.raises(ProxyUsagePersistenceError):
        async with tracker.track(
            operation="mint",
            transaction_key="missing-trial-client",
            trial_scope=scope,
            **_attribution(scope),
        ):
            raise AssertionError("provider boundary crossed")

    normal.rpc.assert_not_called()
    normal.from_.assert_not_called()


@pytest.mark.asyncio
async def test_trial_zero_byte_operation_releases_via_trial_rpc_without_event_insert():
    normal = _supabase()
    trial = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(normal, trial_supabase=trial, enabled=True)

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response({
            "allowed": True,
            "reservation_id": "66666666-6666-4666-8666-666666666666",
            "claim_status": "claimed",
            "blocking_scope": None,
        }),
        _response(True),
    ]):
        async with tracker.track(
            operation="mint",
            transaction_key="trial-zero-byte",
            trial_scope=scope,
            **_attribution(scope),
        ):
            pass

    assert [call.args[0] for call in trial.rpc.call_args_list] == [
        "pjud_proxy_reserve_trial_budget",
        "pjud_proxy_finalize_trial_budget_reservation",
    ]
    assert trial.rpc.call_args_list[-1].args[1]["p_release"] is True
    normal.from_.assert_not_called()
    trial.from_.assert_not_called()


@pytest.mark.asyncio
async def test_trial_already_reserved_without_exact_lineage_stops_before_provider():
    normal = _supabase()
    trial = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(normal, trial_supabase=trial, enabled=True)
    entered = False

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response({
            "allowed": True,
            "reservation_id": "66666666-6666-4666-8666-666666666666",
            "claim_status": "already_reserved",
            "blocking_scope": None,
        }),
        _response([]),
        _response(True),
    ]):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="search",
                transaction_key="unowned-replay",
                trial_scope=scope,
                **_attribution(scope),
            ):
                entered = True

    assert entered is False
    assert trial.rpc.call_count == 1
    assert normal.from_.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", ["true", 1, None])
async def test_trial_reservation_requires_a_boolean_decision_before_provider(allowed):
    normal = _supabase()
    trial = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(normal, trial_supabase=trial, enabled=True)
    entered = False

    with patch("worker.proxy_usage.run_query", return_value=_response({
        "allowed": allowed,
        "reservation_id": "66666666-6666-4666-8666-666666666666",
        "claim_status": "claimed",
        "blocking_scope": None,
    })):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="search",
                transaction_key="invalid-decision",
                trial_scope=scope,
                **_attribution(scope),
            ):
                entered = True

    assert entered is False
    normal.from_.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reservation",
    [
        {
            "allowed": True,
            "reservation_id": "66666666-6666-4666-8666-666666666666",
            "claim_status": "unexpected-weaker-contract",
        },
        {
            "allowed": True,
            "reservation_id": "66666666-6666-4666-8666-666666666666",
            "claim_status": "claimed",
            "unexpected": "field",
        },
        {
            "allowed": True,
            "reservation_id": "66666666-6666-4666-8666-666666666666",
            "claim_status": "already_unresolved",
        },
    ],
)
async def test_trial_reservation_unknown_status_or_keyset_fails_before_provider(
    reservation,
):
    """A weaker or drifted reserve contract must never authorize traffic."""
    normal = _supabase()
    trial = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(normal, trial_supabase=trial, enabled=True)
    entered = False

    with patch(
        "worker.proxy_usage.run_query",
        return_value=_response(reservation),
    ):
        with pytest.raises(ProxyUsagePersistenceError):
            async with tracker.track(
                operation="search",
                transaction_key="drifted-reserve-contract",
                trial_scope=scope,
                **_attribution(scope),
            ):
                entered = True

    assert entered is False
    assert trial.rpc.call_count == 1
    normal.from_.assert_not_called()


@pytest.mark.asyncio
async def test_trial_already_reserved_reconciles_exact_lineage_before_provider():
    normal = _supabase()
    trial = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(normal, trial_supabase=trial, enabled=True)
    reservation_id = "66666666-6666-4666-8666-666666666666"
    entered = False
    query_count = 0

    async def execute(_query):
        nonlocal query_count
        query_count += 1
        if query_count == 1:
            return _response({
                "allowed": True,
                "reservation_id": reservation_id,
                "claim_status": "already_reserved",
            })
        if query_count == 2:
            claim = trial.rpc.call_args.args[1]["p_reservation_claim_token"]
            return _response([{
                "id": reservation_id,
                "claim_token": claim,
                "status": "reserved",
                "blocking_scope": None,
                "law_firm_id": str(scope.law_firm_id),
                "trial_grant_id": str(scope.trial_grant_id),
                "import_job_id": str(scope.job_id),
                "import_claim_token": str(scope.claim_token),
                "import_worker_id": scope.worker_id,
            }])
        if query_count == 3:
            return _response("77777777-7777-4777-8777-777777777777")
        return _response(True)

    with patch("worker.proxy_usage.run_query", side_effect=execute):
        async with tracker.track(
            operation="search",
            transaction_key="owned-reserve-replay",
            trial_scope=scope,
            **_attribution(scope),
        ):
            entered = True

    assert entered is True
    assert normal.from_.call_args_list[0].args[0] == (
        "pjud_proxy_budget_reservations"
    )


@pytest.mark.asyncio
async def test_trial_unknown_operation_stops_before_reserve_or_provider():
    normal = _supabase()
    trial = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(normal, trial_supabase=trial, enabled=True)
    entered = False

    with pytest.raises(ProxyUsagePersistenceError):
        async with tracker.track(
            operation="detail",
            transaction_key="out-of-scope-operation",
            trial_scope=scope,
            **_attribution(scope),
        ):
            entered = True

    assert entered is False
    trial.rpc.assert_not_called()
    normal.from_.assert_not_called()


@pytest.mark.asyncio
async def test_trial_ambiguous_reserve_recovers_only_exact_full_lineage():
    normal = _supabase()
    trial = _supabase()
    scope = _scope()
    tracker = ProxyUsageTracker(normal, trial_supabase=trial, enabled=True)
    reservation_id = "66666666-6666-4666-8666-666666666666"
    observed_provider = False
    query_count = 0

    async def execute(_query):
        nonlocal query_count
        query_count += 1
        if query_count == 1:
            raise httpx.ReadTimeout("response lost")
        if query_count == 2:
            # The random reservation claim is part of both the read filter and
            # the immutable row. Mirror it into this read-only fake result.
            claim = trial.rpc.call_args.args[1]["p_reservation_claim_token"]
            return _response([{
                "id": reservation_id,
                "claim_token": claim,
                "status": "reserved",
                "blocking_scope": None,
                "law_firm_id": str(scope.law_firm_id),
                "trial_grant_id": str(scope.trial_grant_id),
                "import_job_id": str(scope.job_id),
                "import_claim_token": str(scope.claim_token),
                "import_worker_id": scope.worker_id,
            }])
        return _response(True)

    with patch("worker.proxy_usage.run_query", side_effect=execute):
        async with tracker.track(
            operation="search",
            transaction_key="ambiguous-exact-recovery",
            trial_scope=scope,
            **_attribution(scope),
        ):
            observed_provider = True

    assert observed_provider is True
    selected = normal.from_.return_value.select.call_args.args[0]
    assert "law_firm_id" in selected
    normal.from_.return_value.eq.assert_any_call(
        "law_firm_id", str(scope.law_firm_id),
    )
