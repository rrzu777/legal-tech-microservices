from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request

from app.auth import verify_api_key
from app.config import get_settings
from app.errors import safe_error
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

    # El bundle F5 se pide FUERA del `try` del timeout y antes de contar el
    # request, igual que `acquire_or_alert` en search y detail: si el pool no
    # tiene con qué salir, esto lanza 503 y no contesta 200 con `blocked`.
    pool = request.app.state.session_pool
    bundle = await familia_bundle_or_alert(pool, request)

    api_metrics.record_request("familia")

    try:
        async with asyncio.timeout(_SYNC_TIMEOUT_S):
            proxy_usage = getattr(request.app.state, "proxy_usage", DISABLED_PROXY_USAGE)
            async with proxy_usage.track(operation="search") as usage:
                resp = await _run_sync(
                    req,
                    rate_s,
                    bundle,
                    proxy_control=getattr(request.app.state, "proxy_control", None),
                    proxy_usage_capture=usage,
                )
                if not resp.ok and resp.error_code == "blocked":
                    usage.status = "blocked"
                    usage.error_kind = "ojv"
    except TimeoutError:
        logger.warning("familia_sync: timed out after %ds", _SYNC_TIMEOUT_S)
        api_metrics.record_error("familia")
        return FamiliaSyncResponse(
            ok=False, casos=[],
            error_code="session_error",
            error="La operación excedió el tiempo máximo permitido",
        )

    # Las métricas de esta ruta son nuevas: hasta acá `familia.py` no tenía UNA
    # sola llamada a `api_metrics`, así que el agujero que se cerró para search y
    # detail seguía abierto justo en la ruta cuya respuesta la app malinterpreta.
    if resp.ok:
        api_metrics.record_success("familia")
    else:
        api_metrics.record_error("familia")
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
    proxy_usage_capture=None,
) -> FamiliaSyncResponse:
    async with FamiliaAuthSession(
        bundle.proxy_url, bundle.cookies, bundle.user_agent, rate_limit_s=rate_s,
    ) as session:
        try:
            await session.login(req.rut, req.password, req.auth_type)
        except FamiliaBlockedError:
            return _blocked()
        except InvalidCredentialsError:
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="invalid_credentials",
                error="Las credenciales proporcionadas no son válidas",
            )
        except OjvSessionError as e:
            logger.warning("familia_sync: session error: %s", safe_error(e))
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="session_error",
                error="No se pudo establecer sesión con OJV",
            )
        except Exception as e:
            billing = await _proxy_billing_response(e, proxy_control)
            if billing:
                if proxy_usage_capture is not None:
                    proxy_usage_capture.status = "error"
                    proxy_usage_capture.error_kind = "billing"
                return billing
            emit_private_event(
                logger, event="private_session", status="failed",
                error_code="upstream_changed", stage="login",
            )
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="session_error",
                error="No se pudo establecer sesión con OJV",
            )

        if req.cases:
            all_casos = []
            for case_filter in req.cases:
                try:
                    html = await session.search_familia(
                        rut=req.rut, rit=case_filter.rit, year=case_filter.year,
                    )
                    casos, err = parse_familia_results(html)
                    if err and err != "no_cases":
                        logger.warning("familia_sync: parse error code=%s", err)
                    all_casos.extend(casos)
                except FamiliaBlockedError:
                    # Un bloqueo F5 aborta el batch: no seguir martillando la
                    # misma IP bloqueada ni reportar ok=True ocultando el bloqueo.
                    return _blocked()
                except OjvSessionError as e:
                    logger.warning(
                        "familia_sync: authenticated session failure code=%s",
                        e.code.value,
                    )
                    return FamiliaSyncResponse(
                        ok=False,
                        casos=[],
                        error_code="session_error",
                        error="No se pudo consultar la causa en OJV",
                    )
                except Exception as e:
                    billing = await _proxy_billing_response(e, proxy_control)
                    if billing:
                        if proxy_usage_capture is not None:
                            proxy_usage_capture.status = "error"
                            proxy_usage_capture.error_kind = "billing"
                        return billing
                    # El mismo criterio que el bloqueo, extendido a las otras
                    # fallas que tampoco son de la causa. Este `except` se tragaba
                    # TODO y seguía, y después devolvía `ok=True` con `casos=[]` y
                    # sin `error_code` — indistinguible de "esa causa no está en
                    # el portal". La app entonces le sumaba una falla a la causa, y
                    # a las 10 la suspendía por un proxy que se cayó medio segundo.
                    #
                    # Y es el camino que importa: la app SIEMPRE manda `cases`
                    # con un RIT, así que esta rama es la única que se ejecuta en
                    # producción. La de abajo —consultar todas las causas del
                    # RUT— hoy no la usa nadie.
                    kind = classify_exception(e)
                    if kind == "ojv":
                        return _blocked()
                    if kind == "infra":
                        emit_private_event(
                            logger, event="private_session", status="failed",
                            error_code="upstream_changed", stage="detail",
                        )
                        return FamiliaSyncResponse(
                            ok=False, casos=[],
                            error_code="session_error",
                            error="No se pudo consultar la causa en OJV",
                        )
                    emit_private_event(
                        logger, event="private_session", status="failed",
                        error_code="upstream_changed", stage="detail",
                    )
            return FamiliaSyncResponse(ok=True, casos=all_casos)

        try:
            html = await session.search_familia(rut=req.rut)
        except FamiliaBlockedError:
            return _blocked()
        except OjvSessionError as e:
            logger.warning(
                "familia_sync: authenticated session failure code=%s",
                e.code.value,
            )
            return FamiliaSyncResponse(
                ok=False,
                casos=[],
                error_code="session_error",
                error="No se pudo consultar OJV",
            )
        except Exception as e:
            billing = await _proxy_billing_response(e, proxy_control)
            if billing:
                if proxy_usage_capture is not None:
                    proxy_usage_capture.status = "error"
                    proxy_usage_capture.error_kind = "billing"
                return billing
            emit_private_event(
                logger, event="private_session", status="failed",
                error_code="upstream_changed", stage="detail",
            )
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="session_error",
                error="No se pudo consultar OJV",
            )

        all_casos, error_code = parse_familia_results(html)

    if error_code == "parse_error":
        return FamiliaSyncResponse(
            ok=False, casos=[],
            error_code="parse_error",
            error="No se pudo interpretar la respuesta de OJV",
        )

    # no_cases is a valid successful result (login OK, user has 0 Familia cases)
    return FamiliaSyncResponse(ok=True, casos=all_casos)
