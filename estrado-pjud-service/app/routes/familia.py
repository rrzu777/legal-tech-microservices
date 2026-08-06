from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.config import get_settings
from app.errors import safe_error
from app.failure_kind import classify_exception
from app.familia.auth import FamiliaAuthSession, FamiliaBlockedError, InvalidCredentialsError, SessionError
from app.familia.models import FamiliaSyncRequest, FamiliaSyncResponse
from app.familia.parser import parse_familia_results
from app.metrics import api_metrics
from app.pool_guard import familia_bundle_or_alert, record_blocked_and_alert
from app.proxy_billing import is_proxy_billing_error
from app.rate_limit import limiter
from worker.proxy_usage import DISABLED_PROXY_USAGE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/familia", tags=["familia"])

# Familia sync is expensive: login + N queries at 2.5s each. Limit to 2/min per key.
_SYNC_TIMEOUT_S = 60


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
        except SessionError as e:
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
            logger.exception("familia_sync: unexpected error during login")
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="session_error",
                error=safe_error(e),
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
                        logger.warning(
                            "familia_sync: parse error for RIT %s-%s: %s",
                            case_filter.rit, case_filter.year, err,
                        )
                    all_casos.extend(casos)
                except FamiliaBlockedError:
                    # Un bloqueo F5 aborta el batch: no seguir martillando la
                    # misma IP bloqueada ni reportar ok=True ocultando el bloqueo.
                    return _blocked()
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
                        logger.warning(
                            "familia_sync: fallo de infra en RIT %s: %s",
                            case_filter.rit, safe_error(e),
                        )
                        return FamiliaSyncResponse(
                            ok=False, casos=[],
                            error_code="session_error",
                            error=safe_error(e),
                        )
                    logger.warning("familia_sync: error querying RIT %s: %s", case_filter.rit, safe_error(e))
            return FamiliaSyncResponse(ok=True, casos=all_casos)

        try:
            html = await session.search_familia(rut=req.rut)
        except FamiliaBlockedError:
            return _blocked()
        except Exception as e:
            billing = await _proxy_billing_response(e, proxy_control)
            if billing:
                if proxy_usage_capture is not None:
                    proxy_usage_capture.status = "error"
                    proxy_usage_capture.error_kind = "billing"
                return billing
            logger.exception("familia_sync: unexpected error querying Familia")
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="session_error",
                error=safe_error(e),
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
