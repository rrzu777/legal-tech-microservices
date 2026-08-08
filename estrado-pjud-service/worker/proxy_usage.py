"""Durable, tenant-attributed proxy usage and atomic budget reservations."""

from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from app.bandwidth import ProxyUsageCapture, capture_proxy_usage
from app.proxy_billing import is_proxy_billing_error
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.usage_context import current_usage_scope
from worker.config import run_query

logger = logging.getLogger(__name__)

DEFAULT_PRICE_PER_GB_USD = 6.25
ESTIMATED_OPERATION_BYTES = {
    # Conservative upper envelopes: finalization intentionally pauses traffic
    # when actual spend exceeds a reservation, so estimates must not be means.
    "search": 2_000_000,
    "detail": 5_000_000,
    "document_primary": 12_000_000,
    "certificate": 12_000_000,
    "anexo_list": 1_000_000,
    "anexo_document": 12_000_000,
    "mint": 10_000_000,
    "catalog": 2_000_000,
    "opportunistic_catalog_refresh": 2_000_000,
    "health": 1_000_000,
    "other": 5_000_000,
}


class ProxyUsageTracker:
    def __init__(
        self,
        supabase,
        *,
        enabled: bool,
        component: str = "worker",
        provider: str = "iproyal",
        price_per_gb_usd: float = DEFAULT_PRICE_PER_GB_USD,
    ):
        self._sb = supabase
        self._enabled = enabled
        self._component = component
        self._provider = provider
        self._price_per_gb_usd = price_per_gb_usd

    @asynccontextmanager
    async def track(
        self,
        *,
        operation: str,
        law_firm_id: str | None = None,
        case_id: str | None = None,
        sync_run_id: str | None = None,
        movement_id: str | None = None,
        transaction_key: str | None = None,
        estimated_bytes: int | None = None,
        cause_operation: Literal["opportunistic_catalog_refresh"] | None = None,
        cause_session_id: uuid.UUID | None = None,
    ):
        inherited = current_usage_scope()
        law_firm_id = law_firm_id or inherited["law_firm_id"]
        case_id = case_id or inherited["case_id"]
        sync_run_id = sync_run_id or inherited["sync_run_id"]
        if not self._enabled:
            with capture_proxy_usage() as usage:
                yield usage
            return

        transaction_key = transaction_key or str(uuid.uuid4())
        idempotency_key = hashlib.sha256(
            f"{self._component}:{operation}:{transaction_key}".encode("utf-8")
        ).hexdigest()
        claim_token = str(uuid.uuid4())
        estimate = (
            ESTIMATED_OPERATION_BYTES.get(operation, ESTIMATED_OPERATION_BYTES["other"])
            if estimated_bytes is None
            else max(0, estimated_bytes)
        )
        estimated_cost = estimate / 1_000_000_000 * self._price_per_gb_usd

        try:
            if self._component == "api" and sync_run_id is not None:
                scope_response = await run_query(
                    self._sb.from_("case_sync_runs")
                    .select("id")
                    .eq("id", sync_run_id)
                    .eq("case_id", case_id)
                    .eq("law_firm_id", law_firm_id)
                    .limit(1)
                )
                scope_rows = (
                    scope_response.data
                    if isinstance(scope_response.data, list)
                    else []
                )
                if len(scope_rows) != 1:
                    raise RuntimeError("sync run attribution does not match case and law firm")
            reserve_response = await run_query(self._sb.rpc(
                "pjud_proxy_reserve_budget",
                {
                    "p_case_id": case_id,
                    "p_law_firm_id": law_firm_id,
                    "p_estimated_cost_usd": estimated_cost,
                    "p_idempotency_key": idempotency_key,
                    "p_claim_token": claim_token,
                    "p_provider": self._provider,
                    "p_operation": operation,
                },
            ))
            reserve_rows = (
                reserve_response.data
                if isinstance(reserve_response.data, list)
                else []
            )
            if len(reserve_rows) != 1:
                raise RuntimeError("budget reservation did not return one row")
            reservation = reserve_rows[0]
        except Exception as exc:
            raise ProxyUsagePersistenceError(
                "proxy budget reservation unavailable"
            ) from exc

        if not reservation.get("allowed"):
            raise ProxyBudgetExceededError(reservation.get("blocking_scope"))

        reservation_id = str(reservation["reservation_id"])
        caught: BaseException | None = None
        with capture_proxy_usage() as usage:
            try:
                yield usage
            except BaseException as exc:
                caught = exc
                raise
            finally:
                try:
                    await self._persist_and_finalize(
                        usage=usage,
                        operation=operation,
                        idempotency_key=idempotency_key,
                        reservation_id=reservation_id,
                        claim_token=claim_token,
                        law_firm_id=law_firm_id,
                        case_id=case_id,
                        sync_run_id=sync_run_id,
                        movement_id=movement_id,
                        cause_operation=cause_operation,
                        cause_session_id=cause_session_id,
                        error=caught,
                        estimated_bytes=estimate,
                    )
                except Exception as exc:
                    if caught is not None:
                        logger.exception(
                            "Proxy usage persistence failed while propagating provider error"
                        )
                    raise ProxyUsagePersistenceError(
                        "proxy usage persistence unavailable"
                    ) from exc

    async def _persist_and_finalize(
        self,
        *,
        usage: ProxyUsageCapture,
        operation: str,
        idempotency_key: str,
        reservation_id: str,
        claim_token: str,
        law_firm_id: str | None,
        case_id: str | None,
        sync_run_id: str | None,
        movement_id: str | None,
        cause_operation: Literal["opportunistic_catalog_refresh"] | None,
        cause_session_id: uuid.UUID | None,
        error: BaseException | None,
        estimated_bytes: int,
    ) -> None:
        has_provider_usage = usage.request_count > 0
        has_savings_event = usage.documents_skipped > 0
        if not has_provider_usage and not has_savings_event:
            await self._finalize(reservation_id, claim_token, release=True)
            return

        if error is not None:
            status = "error"
            error_kind = "billing" if is_proxy_billing_error(error) else "infra"
        elif usage.status is not None:
            status = usage.status
            error_kind = usage.error_kind
        elif has_provider_usage:
            status = "success"
            error_kind = None
        else:
            status = "skipped"
            error_kind = None

        event_reservation_id = reservation_id if has_provider_usage else None
        uses_estimated_floor = has_provider_usage and usage.bytes_down == 0
        payload = {
            "idempotency_key": idempotency_key,
            "reservation_id": event_reservation_id,
            "law_firm_id": law_firm_id,
            "case_id": case_id,
            "sync_run_id": sync_run_id,
            "movement_id": movement_id,
            "cause_operation": cause_operation,
            "cause_session_id": (
                str(cause_session_id) if cause_session_id is not None else None
            ),
            "request_id": str(uuid.uuid4()),
            "component": self._component,
            "operation": operation,
            "bytes_up": usage.bytes_up,
            "bytes_down": usage.bytes_down,
            "estimated_bytes_floor": estimated_bytes if uses_estimated_floor else 0,
            "measurement_status": (
                "estimated_floor" if uses_estimated_floor else "measured"
            ),
            "request_count": usage.request_count,
            "retry_count": usage.retry_count,
            "documents_downloaded": usage.documents_downloaded,
            "documents_skipped": usage.documents_skipped,
            "status": status,
            "error_kind": error_kind,
            "provider": self._provider,
            "price_per_gb_usd": self._price_per_gb_usd,
        }
        insert_response = await run_query(
            self._sb.from_("pjud_proxy_usage_events").insert(payload)
        )
        inserted = insert_response.data if isinstance(insert_response.data, list) else []
        if len(inserted) != 1:
            raise RuntimeError("usage ledger insert did not return one row")

        await self._finalize(
            reservation_id,
            claim_token,
            release=not has_provider_usage,
        )

    async def _finalize(
        self, reservation_id: str, claim_token: str, *, release: bool,
    ) -> None:
        await run_query(self._sb.rpc(
            "pjud_proxy_finalize_budget_reservation",
            {
                "p_reservation_id": reservation_id,
                "p_claim_token": claim_token,
                "p_release": release,
            },
        ))


DISABLED_PROXY_USAGE = ProxyUsageTracker(None, enabled=False, component="api")
