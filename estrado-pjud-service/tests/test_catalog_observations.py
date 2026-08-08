from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app.catalog_observations import (
    CatalogClaim,
    CatalogObservation,
    CatalogObservationRepository,
    CatalogRefreshIntent,
    canonical_catalog_options,
    catalog_options_hash,
    CatalogOptionConflictError,
    is_partial_catalog,
)


def _intent() -> CatalogRefreshIntent:
    return CatalogRefreshIntent(
        slice_key="tribunals:civil:90",
        catalog="tribunals",
        competencia="civil",
        corte=90,
        anno=None,
        law_firm_id="11111111-1111-4111-8111-111111111111",
        case_id="22222222-2222-4222-8222-222222222222",
        sync_run_id=None,
        request_hash="a" * 64,
    )


def test_catalog_options_are_canonical_and_hash_is_order_independent():
    first = [
        {"code": " 2 ", "label": "  Segundo   Juzgado "},
        {"code": "1", "label": "Primer Juzgado"},
        {"code": "1", "label": "Primer   Juzgado"},
    ]
    second = [
        {"label": "Primer Juzgado", "code": "1"},
        {"label": "Segundo Juzgado", "code": "2"},
    ]

    assert canonical_catalog_options(first) == second
    assert catalog_options_hash(first) == catalog_options_hash(second)


def test_duplicate_codes_choose_the_same_label_regardless_of_response_order():
    first = [
        {"code": "1", "label": "Alfa"},
        {"code": "1", "label": "Alfa"},
    ]
    second = list(reversed(first))

    assert catalog_options_hash(first) == catalog_options_hash(second)


def test_conflicting_duplicate_codes_are_rejected_not_first_wins():
    with pytest.raises(CatalogOptionConflictError):
        canonical_catalog_options([
            {"code": "1", "label": "Alfa"},
            {"code": "1", "label": "Zeta"},
        ])


def test_missing_snapshot_codes_are_always_partial_but_additions_are_not():
    snapshot = [
        {"code": "1", "label": "Uno"},
        {"code": "2", "label": "Dos"},
    ]

    assert is_partial_catalog(snapshot, [{"code": "1", "label": "Uno"}]) is True
    assert is_partial_catalog(
        snapshot,
        [*snapshot, {"code": "3", "label": "Tres"}],
    ) is False


@pytest.mark.asyncio
async def test_repository_uses_run_query_and_never_logs_rpc_payloads():
    rpc = MagicMock(return_value=object())
    supabase = MagicMock(rpc=rpc)
    response = SimpleNamespace(data=[{
        "allowed": True,
        "reason": "claimed",
        "lease_expires_at": "2026-08-07T13:00:00Z",
    }])

    with (
        patch("app.catalog_observations.run_query", return_value=response) as run_query,
        patch("app.catalog_observations.logger") as logger,
    ):
        claim = await CatalogObservationRepository(supabase).claim(_intent())

    assert claim is not None
    assert claim.slice_key == "tribunals:civil:90"
    run_query.assert_awaited_once_with(rpc.return_value)
    logger.debug.assert_not_called()
    logger.info.assert_not_called()


@pytest.mark.asyncio
async def test_claim_denial_telemetry_failure_is_visible_to_queue():
    supabase = MagicMock()
    supabase.rpc.return_value = object()
    with patch(
        "app.catalog_observations.run_query",
        side_effect=[
            SimpleNamespace(data=[{
                "allowed": False,
                "reason": "lease_busy",
                "lease_expires_at": None,
            }]),
            RuntimeError("telemetry unavailable"),
        ],
    ):
        with pytest.raises(RuntimeError, match="telemetry unavailable"):
            await CatalogObservationRepository(supabase).claim(_intent())


@pytest.mark.asyncio
async def test_repository_reads_persistent_control_without_exposing_payload():
    query = MagicMock()
    supabase = MagicMock()
    supabase.from_.return_value.select.return_value.eq.return_value.limit.return_value = query
    response = SimpleNamespace(data=[{
        "opportunistic_enabled": True,
        "circuit_open": False,
    }])

    with patch("app.catalog_observations.run_query", return_value=response) as run_query:
        control = await CatalogObservationRepository(supabase).control()

    assert control.opportunistic_enabled is True
    assert control.circuit_open is False
    run_query.assert_awaited_once_with(query)


@pytest.mark.asyncio
async def test_repository_complete_preserves_distinct_session_for_rpc_quorum():
    supabase = MagicMock()
    supabase.rpc.return_value = object()
    generation = UUID("33333333-3333-4333-8333-333333333333")
    observation = CatalogObservation(
        snapshot_hash="b" * 64,
        snapshot_options=[{"code": "1", "label": "Uno"}],
        observed_hash="c" * 64,
        options=[{"code": "1", "label": "Uno"}],
        session_generation_id=generation,
        bytes_up=10,
        bytes_down=20,
        partial=True,
        confirmed_by_full_refresh=True,
    )
    claim_response = SimpleNamespace(data=[{
        "allowed": True,
        "reason": "claimed",
        "lease_expires_at": None,
    }])

    with patch(
        "app.catalog_observations.run_query",
        side_effect=[claim_response, SimpleNamespace(data=[{"completed": True}])],
    ):
        repository = CatalogObservationRepository(supabase)
        claim = await repository.claim(_intent())
        await repository.complete(claim, observation)

    complete_payload = supabase.rpc.call_args_list[1].args[1]
    assert complete_payload["p_session_generation_id"] == str(generation)
    assert complete_payload["p_confirmed_by_full_refresh"] is False
    assert complete_payload["p_snapshot_options"] == [{"code": "1", "label": "Uno"}]
    assert complete_payload["p_options"] == [{"code": "1", "label": "Uno"}]


@pytest.mark.asyncio
async def test_repository_uses_atomic_retirement_breaker_rpc_contract():
    supabase = MagicMock()
    supabase.rpc.return_value = object()
    originating_intent = _intent()
    catalog_claim = CatalogClaim(
        slice_key=originating_intent.slice_key,
        token=UUID("44444444-4444-4444-8444-444444444444"),
        lease_expires_at=None,
        intent=originating_intent,
    )
    generation = UUID("33333333-3333-4333-8333-333333333333")

    with patch(
        "app.catalog_observations.run_query",
        return_value=SimpleNamespace(data=[{
            "claim_released": True,
            "circuit_newly_opened": True,
            "events_inserted": 2,
        }]),
    ) as run_query:
        await CatalogObservationRepository(supabase).retire_and_open_circuit(
            catalog_claim,
            originating_intent,
            "invalid_catalog",
            generation,
        )

    assert supabase.rpc.call_args.args == (
        "pjud_catalog_retire_and_open_circuit",
        {
            "p_slice_key": "tribunals:civil:90",
            "p_claim_token": "44444444-4444-4444-8444-444444444444",
            "p_reason": "invalid_catalog",
            "p_session_generation_id": str(generation),
            "p_law_firm_id": originating_intent.law_firm_id,
            "p_case_id": originating_intent.case_id,
            "p_request_hash": "a" * 64,
        },
    )
    run_query.assert_awaited_once_with(supabase.rpc.return_value)
