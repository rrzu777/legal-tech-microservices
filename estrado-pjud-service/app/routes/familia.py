from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request

from app.auth import verify_api_key
from app.config import get_settings
from app.failure_kind import classify_exception
from app.familia.auth import FamiliaAuthSession, FamiliaBlockedError, InvalidCredentialsError
from app.familia.models import (
    FamiliaSyncRequest,
    FamiliaSyncResponse,
    PrivateCauseResolutionRequest,
    PrivateCauseResolutionResult,
)
from app.familia.parser import (
    PrivateResolutionError,
    parse_familia_results,
    resolve_private_familia_html,
)
from app.ojv.errors import OjvSessionError
from app.ojv.private_telemetry import (
    emit_private_event,
    private_operational_metrics,
)
from app.metrics import api_metrics
from app.pool_guard import familia_bundle_or_alert, record_blocked_and_alert
from app.proxy_billing import is_proxy_billing_error
from app.rate_limit import limiter
from worker.proxy_usage import DISABLED_PROXY_USAGE
from app.usage_context import current_usage_scope
from worker.sync_credentials import (
    SyncCredentialClient, SyncCredentialClaimStaleError, SyncCredentialInfrastructureError,
)

logger = logging.getLogger(__name__)

_PRIVATE_RESOLUTION_CODES = frozenset((
    "private_not_found", "private_ambiguous", "private_identifier_mismatch",
    "private_tribunal_mismatch", "private_evidence_incomplete",
    "private_fence_unavailable", "credential_invalid", "session_expired",
    "waf", "timeout", "upstream_changed",
))


def _closed_private_resolution_code(error: PrivateResolutionError) -> str:
    value = error.args[0] if len(error.args) == 1 else None
    return value if isinstance(value, str) and value in _PRIVATE_RESOLUTION_CODES else "upstream_changed"

router = APIRouter(prefix="/api/v1/familia", tags=["familia"])

# Familia sync is expensive: login + N queries at 2.5s each. Limit to 2/min per key.
_SYNC_TIMEOUT_S = 60
PrivateStageGuard = Callable[[str], Awaitable[bool]]


def _private_failure(code: str) -> PrivateCauseResolutionResult:
    return PrivateCauseResolutionResult(ok=False, resolution=None, error_code=code)


async def _run_private_resolution(
    req: PrivateCauseResolutionRequest,
    rate_s: float,
    bundle,
    catalog_service,
    stage_guard: PrivateStageGuard | None = None,
) -> PrivateCauseResolutionResult:
    async with FamiliaAuthSession(
        bundle.proxy_url, bundle.cookies, bundle.user_agent, rate_limit_s=rate_s,
    ) as session:
        try:
            await session.login(req.rut, req.password, req.auth_type)
            if stage_guard is None or not await stage_guard("login"):
                private_operational_metrics.record_result("lease_loss")
                emit_private_event(
                    logger, event="private_resolution", status="failed",
                    error_code="private_fence_unavailable", stage="login",
                )
                return _private_failure("private_fence_unavailable")
            prefix, number, year = req.case_number.split("-", 2)
            if not prefix or not number.isdigit() or not year.isdigit():
                return _private_failure("private_identifier_mismatch")
            html = await session.search_familia(rut=req.rut, rit=number, year=year)

            def resolve_tribunal(label: str) -> int | None:
                identity = catalog_service.resolve_loaded_tribunal("familia", label)
                return identity.tribunal_code if identity is not None else None

            resolution = resolve_private_familia_html(
                html,
                expected_case_number=req.case_number,
                expected_tribunal_code=req.tribunal_code,
                expected_tribunal_label=req.tribunal_label,
                resolve_tribunal=resolve_tribunal,
            )
            if not await stage_guard("detail"):
                private_operational_metrics.record_result("lease_loss")
                emit_private_event(
                    logger, event="private_resolution", status="failed",
                    error_code="private_fence_unavailable", stage="detail",
                )
                return _private_failure("private_fence_unavailable")
            # Movement/enrichment remains a separate guarded boundary even
            # though Familia is listing-only and therefore cannot reach it yet.
            movements = resolution.movements
            if not await stage_guard("movements"):
                private_operational_metrics.record_result("lease_loss")
                emit_private_event(
                    logger, event="private_resolution", status="failed",
                    error_code="private_fence_unavailable", stage="movements",
                )
                return _private_failure("private_fence_unavailable")
            resolution = resolution.model_copy(update={"movements": movements})
            html = ""
            return PrivateCauseResolutionResult(
                ok=True,
                resolution=resolution,
                error_code=None,
            )
        except PrivateResolutionError as error:
            code = _closed_private_resolution_code(error)
            if code == "upstream_changed":
                private_operational_metrics.record_result("upstream_changed")
                emit_private_event(
                    logger, event="private_resolution", status="failed",
                    error_code="upstream_changed", stage="detail",
                )
            return _private_failure(code)
        except OjvSessionError as error:
            private_operational_metrics.record_result(error.code.value)
            emit_private_event(
                logger, event="private_resolution", status="failed",
                error_code=error.code.value, stage="login",
            )
            return _private_failure(error.code.value)
        except asyncio.CancelledError:
            emit_private_event(
                logger, event="private_session", status="cancelled",
                error_code="cancelled", stage="shutdown",
            )
            raise
        except Exception:
            private_operational_metrics.record_result("upstream_changed")
            emit_private_event(
                logger, event="private_resolution", status="failed",
                error_code="upstream_changed", stage="login",
            )
            return _private_failure("upstream_changed")


@router.post("/resolve-private", response_model=PrivateCauseResolutionResult)
@limiter.limit("2/minute")
async def familia_resolve_private(
    request: Request,
    req: PrivateCauseResolutionRequest,
    _api_key: str = verify_api_key,
) -> PrivateCauseResolutionResult:
    settings = get_settings()
    if not settings.private_familia_enabled:
        raise HTTPException(status_code=404, detail="private_familia_disabled")
    private_operational_metrics.record_attempt()
    async with request.app.state.private_resolution_budget.slot():
        bundle = await familia_bundle_or_alert(request.app.state.session_pool, request)
        api_metrics.record_request("familia")
        try:
            async with asyncio.timeout(_SYNC_TIMEOUT_S):
                result = await _run_private_resolution(
                    req,
                    settings.RATE_LIMIT_MS / 1000.0,
                    bundle,
                    request.app.state.catalog_service,
                )
        except TimeoutError:
            private_operational_metrics.record_result("timeout")
            emit_private_event(
                logger, event="private_resolution", status="failed",
                error_code="timeout", stage="detail",
            )
            result = _private_failure("timeout")
    if result.ok:
        api_metrics.record_success("familia")
    else:
        api_metrics.record_error("familia")
    return result


@router.post("/sync", response_model=FamiliaSyncResponse)
@limiter.limit("2/minute")
async def familia_sync(
    request: Request,
    req: FamiliaSyncRequest,
    _api_key: str = verify_api_key,
) -> FamiliaSyncResponse:
    settings = get_settings()
    rate_s = settings.RATE_LIMIT_MS / 1000.0

    scope = current_usage_scope()
    if (scope.get("law_firm_id") != req.sync_claim.law_firm_id
            or scope.get("case_id") != req.sync_claim.case_id
            or scope.get("sync_run_id") != req.sync_claim.run_id
            or scope.get("lookup_attempt_id") is not None):
        raise HTTPException(status_code=403, detail="invalid_sync_credential_claim")
    claims = SyncCredentialClient(getattr(request.app.state, "proxy_supabase", None))
    try:
        # Check before resource acquisition, again after any pool wait before
        # login, then immediately before search inside _run_sync.
        await claims.check(req.sync_claim, req.credential_version)
        bundle = await familia_bundle_or_alert(request.app.state.session_pool, request)
        api_metrics.record_request("familia")
        async with asyncio.timeout(_SYNC_TIMEOUT_S):
            proxy_usage = getattr(request.app.state, "proxy_usage", DISABLED_PROXY_USAGE)
            async with proxy_usage.track(operation="search") as usage:
                resp = await _run_sync(
                    req, rate_s, bundle, claims=claims,
                    proxy_control=getattr(request.app.state, "proxy_control", None),
                    proxy_usage_capture=usage,
                )
                if not resp.ok and resp.error_code == "blocked":
                    usage.status = "blocked"
                    usage.error_kind = "ojv"
    except SyncCredentialClaimStaleError:
        return FamiliaSyncResponse(ok=False, casos=[], error_code="sync_claim_stale")
    except (SyncCredentialInfrastructureError, TimeoutError):
        raise HTTPException(status_code=503, detail="infra_unavailable") from None

    # Las métricas de esta ruta son nuevas: hasta acá `familia.py` no tenía UNA
    # sola llamada a `api_metrics`, así que el agujero que se cerró para search y
    # detail seguía abierto justo en la ruta cuya respuesta la app malinterpreta.
    if resp.ok:
        api_metrics.record_success("familia")
    else:
        api_metrics.record_error("familia")
        if resp.error_code == "session_error":
            raise HTTPException(status_code=503, detail="infra_unavailable")
        if resp.error_code == "blocked":
            await record_blocked_and_alert(request, "familia")

    return resp


_BLOCKED_MSG = "OJV está limitando el acceso; reintentá en unos minutos"


def _blocked() -> FamiliaSyncResponse:
    """Sin parametro: el unico call site que pasaba otro texto era el del pool
    sin bundle F5, y ese ya no vive aca —sale 503 por `familia_bundle_or_alert`,
    porque era una caida NUESTRA disfrazada de bloqueo del portal."""
    return FamiliaSyncResponse(ok=False, casos=[], error_code="blocked", error=_BLOCKED_MSG)


async def _proxy_billing_response(error: Exception, proxy_control) -> FamiliaSyncResponse | None:
    if not is_proxy_billing_error(error):
        return None
    if proxy_control is not None:
        await proxy_control.trip_billing_exhausted()
    return FamiliaSyncResponse(
        ok=False,
        casos=[],
        error_code="session_error",
        error="Servicio de sincronizacion temporalmente no disponible",
    )


async def _run_sync(
    req: FamiliaSyncRequest, rate_s: float, bundle, proxy_control=None,
    proxy_usage_capture=None, *, claims: SyncCredentialClient,
) -> FamiliaSyncResponse:
    async with FamiliaAuthSession(
        bundle.proxy_url, bundle.cookies, bundle.user_agent, rate_limit_s=rate_s,
    ) as session:
        await claims.check(req.sync_claim, req.credential_version)
        try:
            await session.login(req.rut, req.password, req.auth_type)
        except InvalidCredentialsError:
            return FamiliaSyncResponse(ok=False, casos=[], error_code="invalid_credentials",
                                       error="Las credenciales proporcionadas no son válidas")
        except FamiliaBlockedError:
            return _blocked()
        except Exception as error:
            billing = await _proxy_billing_response(error, proxy_control)
            if billing:
                if proxy_usage_capture is not None:
                    proxy_usage_capture.status = "error"
                    proxy_usage_capture.error_kind = "billing"
                return billing
            return FamiliaSyncResponse(ok=False, casos=[], error_code="session_error",
                                       error="No se pudo establecer sesión con OJV")

        await claims.check(req.sync_claim, req.credential_version)
        # The closed model enforces exactly one filter: there is no all-causes
        # or legacy batch mode under a singular case reservation.
        case_filter = req.cases[0]
        try:
            html = await session.search_familia(
                rut=req.rut, rit=case_filter.rit, year=case_filter.year,
            )
        except FamiliaBlockedError:
            return _blocked()
        except Exception as error:
            billing = await _proxy_billing_response(error, proxy_control)
            if billing:
                if proxy_usage_capture is not None:
                    proxy_usage_capture.status = "error"
                    proxy_usage_capture.error_kind = "billing"
                return billing
            if classify_exception(error) == "ojv":
                return _blocked()
            return FamiliaSyncResponse(ok=False, casos=[], error_code="session_error",
                                       error="No se pudo consultar la causa en OJV")
        try:
            casos, error_code = parse_familia_results(html)
        except Exception:
            return FamiliaSyncResponse(ok=False, casos=[], error_code="session_error")
        finally:
            html = ""
        if error_code and casos:
            return FamiliaSyncResponse(ok=False, casos=[], error_code="session_error")
        if error_code and error_code != "no_cases":
            return FamiliaSyncResponse(ok=False, casos=[], error_code="parse_error")
        return FamiliaSyncResponse(ok=True, casos=casos)
