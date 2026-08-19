"""Durable, tenant-attributed proxy usage and atomic budget reservations."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from postgrest.exceptions import APIError

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
_TELEMETRY_RETRY_DELAYS_S = (0.1, 0.25)
_TRANSIENT_TELEMETRY_API_CODES = frozenset({"40001", "40P01", "55P03"})
_MAX_SESSION_AGE_SECONDS = 86_400
SessionReason = Literal[
    "startup",
    "missing_bundle",
    "soft_age",
    "hard_age",
    "cookie_expired",
    "session_rejected",
    "transport_rotation",
]
_SESSION_REASONS: frozenset[str] = frozenset({
    "startup",
    "missing_bundle",
    "soft_age",
    "hard_age",
    "cookie_expired",
    "session_rejected",
    "transport_rotation",
})
_SESSION_REASONS_WITHOUT_AGE = frozenset({"startup", "missing_bundle"})
_SESSION_OPERATIONS = frozenset({"health", "mint"})


def _is_transient_telemetry_api_error(exc: APIError) -> bool:
    """Only database concurrency outcomes may safely reuse the same claim."""
    return exc.code in _TRANSIENT_TELEMETRY_API_CODES


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

    async def _owned_reservation(
        self, idempotency_key: str, claim_token: str,
    ) -> dict | None:
        response = await run_query(
            self._sb.from_("pjud_proxy_budget_reservations")
            .select("id,claim_token,status,blocking_scope")
            .eq("idempotency_key", idempotency_key)
            .eq("claim_token", claim_token)
            .limit(1)
        )
        rows = response.data if isinstance(response.data, list) else []
        if len(rows) != 1 or rows[0].get("claim_token") != claim_token:
            return None
        return rows[0]

    async def _existing_usage_event(self, payload: dict) -> dict | None:
        response = await run_query(
            self._sb.from_("pjud_proxy_usage_events")
            .select(",".join(payload))
            .eq("idempotency_key", payload["idempotency_key"])
            .limit(1)
        )
        rows = response.data if isinstance(response.data, list) else []
        if not rows:
            return None
        if len(rows) != 1 or any(
            rows[0].get(field) != value for field, value in payload.items()
        ):
            raise RuntimeError("usage ledger immutable event mismatch")
        return rows[0]

    async def _persist_usage_event(self, payload: dict) -> None:
        attempts = len(_TELEMETRY_RETRY_DELAYS_S) + 1
        operation = str(payload["operation"])
        for attempt in range(attempts):
            try:
                query = (
                    self._sb.from_("pjud_proxy_usage_events").insert(payload)
                    if attempt == 0
                    else self._sb.from_("pjud_proxy_usage_events").upsert(
                        payload,
                        on_conflict="idempotency_key",
                        ignore_duplicates=True,
                    )
                )
                response = await run_query(query)
                rows = response.data if isinstance(response.data, list) else []
                if len(rows) == 1:
                    return
                if len(rows) > 1:
                    raise RuntimeError("usage ledger write returned multiple rows")
                if await self._existing_usage_event(payload) is not None:
                    return
            except httpx.TransportError:
                logger.warning(
                    "Transient proxy telemetry boundary=ledger operation=%s "
                    "attempt=%d/%d; reconciling",
                    operation,
                    attempt + 1,
                    attempts,
                )
                try:
                    if await self._existing_usage_event(payload) is not None:
                        return
                except httpx.TransportError:
                    pass

            if attempt == attempts - 1:
                raise RuntimeError(
                    "usage ledger insert outcome could not be reconciled"
                )
            await asyncio.sleep(_TELEMETRY_RETRY_DELAYS_S[attempt])

    async def _reserve_budget(
        self,
        *,
        case_id: str | None,
        law_firm_id: str | None,
        estimated_cost: float,
        idempotency_key: str,
        claim_token: str,
        operation: str,
    ) -> dict:
        payload = {
            "p_case_id": case_id,
            "p_law_firm_id": law_firm_id,
            "p_estimated_cost_usd": estimated_cost,
            "p_idempotency_key": idempotency_key,
            "p_claim_token": claim_token,
            "p_provider": self._provider,
            "p_operation": operation,
        }
        attempts = len(_TELEMETRY_RETRY_DELAYS_S) + 1
        for attempt in range(attempts):
            try:
                response = await run_query(
                    self._sb.rpc("pjud_proxy_reserve_budget", payload)
                )
                rows = response.data if isinstance(response.data, list) else []
                if len(rows) != 1:
                    raise RuntimeError("budget reservation did not return one row")
                reservation = rows[0]
                if reservation.get("claim_status") == "already_reserved":
                    existing = await self._owned_reservation(
                        idempotency_key, claim_token,
                    )
                    if (
                        existing is not None
                        and existing.get("id") == reservation.get("reservation_id")
                        and existing.get("status") == "reserved"
                    ):
                        return {
                            **reservation,
                            "allowed": True,
                            "claim_status": "recovered_reserved",
                        }
                return reservation
            except (httpx.TransportError, APIError) as exc:
                if isinstance(exc, APIError) and not _is_transient_telemetry_api_error(exc):
                    logger.error(
                        "Proxy telemetry reservation rejected operation=%s code=%s; not retrying",
                        operation,
                        exc.code,
                    )
                    raise
                logger.warning(
                    "Transient proxy telemetry boundary=reservation operation=%s "
                    "code=%s attempt=%d/%d; reconciling",
                    operation,
                    exc.code if isinstance(exc, APIError) else "transport",
                    attempt + 1,
                    attempts,
                )
                try:
                    existing = await self._owned_reservation(
                        idempotency_key, claim_token,
                    )
                except httpx.TransportError:
                    existing = None
                except APIError as reconcile_error:
                    if not _is_transient_telemetry_api_error(reconcile_error):
                        logger.error(
                            "Proxy telemetry reservation reconciliation rejected "
                            "operation=%s code=%s; not retrying",
                            operation,
                            reconcile_error.code,
                        )
                        raise
                    existing = None
                if existing is not None:
                    status = existing.get("status")
                    if status in {"reserved", "blocked"}:
                        return {
                            "allowed": status == "reserved",
                            "reservation_id": existing["id"],
                            "claim_status": f"recovered_{status}",
                            "blocking_scope": existing.get("blocking_scope"),
                        }
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(_TELEMETRY_RETRY_DELAYS_S[attempt])

        raise RuntimeError("unreachable telemetry reservation retry state")

    async def _validate_sync_scope(
        self,
        *,
        sync_run_id: str,
        case_id: str | None,
        law_firm_id: str | None,
        operation: str,
    ) -> None:
        attempts = len(_TELEMETRY_RETRY_DELAYS_S) + 1
        for attempt in range(attempts):
            try:
                response = await run_query(
                    self._sb.from_("case_sync_runs")
                    .select("id")
                    .eq("id", sync_run_id)
                    .eq("case_id", case_id)
                    .eq("law_firm_id", law_firm_id)
                    .limit(1)
                )
                rows = response.data if isinstance(response.data, list) else []
                if len(rows) != 1:
                    raise RuntimeError(
                        "sync run attribution does not match case and law firm"
                    )
                return
            except httpx.TransportError:
                logger.warning(
                    "Transient proxy telemetry boundary=scope operation=%s "
                    "attempt=%d/%d",
                    operation,
                    attempt + 1,
                    attempts,
                )
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(_TELEMETRY_RETRY_DELAYS_S[attempt])

    @staticmethod
    def _normalize_session_telemetry(
        *,
        operation: str,
        session_cycle_id: uuid.UUID | None,
        session_reason: SessionReason | None,
        session_age_seconds: int | None,
    ) -> tuple[str | None, SessionReason | None, int | None]:
        has_metadata = any(
            value is not None
            for value in (session_cycle_id, session_reason, session_age_seconds)
        )
        if not has_metadata:
            return None, None, None
        if (
            operation not in _SESSION_OPERATIONS
            or not isinstance(session_cycle_id, uuid.UUID)
            or session_reason not in _SESSION_REASONS
            or (
                session_age_seconds is None
                and session_reason not in _SESSION_REASONS_WITHOUT_AGE
            )
            or (
                session_age_seconds is not None
                and (
                    isinstance(session_age_seconds, bool)
                    or not isinstance(session_age_seconds, int)
                    or session_age_seconds < 0
                )
            )
        ):
            raise ValueError("invalid session telemetry tuple")
        normalized_age = (
            None
            if session_age_seconds is None
            else min(session_age_seconds, _MAX_SESSION_AGE_SECONDS)
        )
        return str(session_cycle_id), session_reason, normalized_age

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
        session_cycle_id: uuid.UUID | None = None,
        session_reason: SessionReason | None = None,
        session_age_seconds: int | None = None,
    ):
        (
            normalized_session_cycle_id,
            normalized_session_reason,
            normalized_session_age_seconds,
        ) = self._normalize_session_telemetry(
            operation=operation,
            session_cycle_id=session_cycle_id,
            session_reason=session_reason,
            session_age_seconds=session_age_seconds,
        )
        inherited = current_usage_scope()
        law_firm_id = law_firm_id or inherited["law_firm_id"]
        case_id = case_id or inherited["case_id"]
        sync_run_id = sync_run_id or inherited["sync_run_id"]
        if not self._enabled:
            with capture_proxy_usage() as usage:
                usage.cause_operation = cause_operation
                usage.cause_session_id = cause_session_id
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
                await self._validate_sync_scope(
                    sync_run_id=sync_run_id,
                    case_id=case_id,
                    law_firm_id=law_firm_id,
                    operation=operation,
                )
            reservation = await self._reserve_budget(
                case_id=case_id,
                law_firm_id=law_firm_id,
                estimated_cost=estimated_cost,
                idempotency_key=idempotency_key,
                claim_token=claim_token,
                operation=operation,
            )
        except Exception as exc:
            raise ProxyUsagePersistenceError(
                "proxy budget reservation unavailable"
            ) from exc

        if not reservation.get("allowed"):
            raise ProxyBudgetExceededError(reservation.get("blocking_scope"))

        reservation_id = str(reservation["reservation_id"])
        caught: BaseException | None = None
        with capture_proxy_usage() as usage:
            usage.cause_operation = cause_operation
            usage.cause_session_id = cause_session_id
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
                        error=caught,
                        estimated_bytes=estimate,
                        session_cycle_id=normalized_session_cycle_id,
                        session_reason=normalized_session_reason,
                        session_age_seconds=normalized_session_age_seconds,
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
        error: BaseException | None,
        estimated_bytes: int,
        session_cycle_id: str | None,
        session_reason: SessionReason | None,
        session_age_seconds: int | None,
    ) -> None:
        has_provider_usage = usage.request_count > 0
        has_savings_event = usage.documents_skipped > 0
        if not has_provider_usage and not has_savings_event:
            await self._finalize(
                reservation_id, claim_token, release=True, operation=operation,
            )
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
            "cause_operation": usage.cause_operation,
            "cause_session_id": (
                str(usage.cause_session_id)
                if usage.cause_session_id is not None
                else None
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
        if session_cycle_id is not None:
            payload.update({
                "session_cycle_id": session_cycle_id,
                "session_reason": session_reason,
                "session_age_seconds": session_age_seconds,
            })
        await self._persist_usage_event(payload)

        # The append-only causal fact already exists once INSERT succeeds. A
        # later reservation-finalize outage must not attribute the same marker
        # to another mint and create a duplicate causal event.
        usage.causal_event_persisted = bool(
            has_provider_usage
            and usage.cause_operation is not None
            and usage.cause_session_id is not None
        )

        await self._finalize(
            reservation_id,
            claim_token,
            release=not has_provider_usage,
            operation=operation,
        )

    async def _finalize(
        self,
        reservation_id: str,
        claim_token: str,
        *,
        release: bool,
        operation: str,
    ) -> None:
        attempts = len(_TELEMETRY_RETRY_DELAYS_S) + 1
        expected_status = "released" if release else "finalized"
        payload = {
            "p_reservation_id": reservation_id,
            "p_claim_token": claim_token,
            "p_release": release,
        }
        for attempt in range(attempts):
            failure: Exception | None = None
            retryable = True
            try:
                await run_query(self._sb.rpc(
                    "pjud_proxy_finalize_budget_reservation", payload,
                ))
                return
            except httpx.TransportError as exc:
                failure = exc
            except APIError as exc:
                if exc.code != "23514":
                    raise
                failure = exc
                retryable = False

            if retryable:
                logger.warning(
                    "Transient proxy telemetry boundary=finalize operation=%s "
                    "attempt=%d/%d; reconciling",
                    operation,
                    attempt + 1,
                    attempts,
                )
            else:
                logger.warning(
                    "Proxy telemetry boundary=finalize operation=%s "
                    "constraint; reconciling once",
                    operation,
                )
            try:
                response = await run_query(
                    self._sb.from_("pjud_proxy_budget_reservations")
                    .select("id,claim_token,status")
                    .eq("id", reservation_id)
                    .eq("claim_token", claim_token)
                    .limit(1)
                )
            except httpx.TransportError:
                response = None

            if response is not None:
                rows = response.data if isinstance(response.data, list) else []
                if len(rows) != 1:
                    raise RuntimeError(
                        "budget finalization reservation ownership was not found"
                    ) from failure
                status = rows[0].get("status")
                if status == expected_status:
                    return
                if status not in {"reserved", "unresolved"}:
                    raise RuntimeError(
                        "budget finalization reached an unexpected terminal state"
                    ) from failure

            if not retryable:
                raise failure

            if attempt == attempts - 1:
                raise RuntimeError(
                    "budget finalization outcome could not be reconciled"
                ) from failure
            await asyncio.sleep(_TELEMETRY_RETRY_DELAYS_S[attempt])


DISABLED_PROXY_USAGE = ProxyUsageTracker(None, enabled=False, component="api")
