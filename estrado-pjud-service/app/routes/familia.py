from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.config import get_settings
from app.errors import safe_error
from app.familia.auth import FamiliaAuthSession, FamiliaBlockedError, InvalidCredentialsError, SessionError
from app.familia.models import FamiliaSyncRequest, FamiliaSyncResponse
from app.familia.parser import parse_familia_results
from app.rate_limit import limiter

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

    try:
        async with asyncio.timeout(_SYNC_TIMEOUT_S):
            pool = request.app.state.session_pool
            return await _run_sync(req, rate_s, pool)
    except TimeoutError:
        logger.warning("familia_sync: timed out after %ds", _SYNC_TIMEOUT_S)
        return FamiliaSyncResponse(
            ok=False, casos=[],
            error_code="session_error",
            error="La operación excedió el tiempo máximo permitido",
        )


async def _run_sync(req: FamiliaSyncRequest, rate_s: float, pool) -> FamiliaSyncResponse:
    bundle = pool.pick_familia_bundle()
    if bundle is None:
        return FamiliaSyncResponse(
            ok=False, casos=[],
            error_code="blocked",
            error="Servicio inicializándose, reintentá en unos minutos",
        )
    async with FamiliaAuthSession(
        bundle.proxy_url, bundle.cookies, bundle.user_agent, rate_limit_s=rate_s,
    ) as session:
        try:
            await session.login(req.rut, req.password, req.auth_type)
        except FamiliaBlockedError:
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="blocked",
                error="OJV está limitando el acceso; reintentá en unos minutos",
            )
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
                    return FamiliaSyncResponse(
                        ok=False, casos=[],
                        error_code="blocked",
                        error="OJV está limitando el acceso; reintentá en unos minutos",
                    )
                except Exception as e:
                    logger.warning("familia_sync: error querying RIT %s: %s", case_filter.rit, safe_error(e))
            return FamiliaSyncResponse(ok=True, casos=all_casos)

        try:
            html = await session.search_familia(rut=req.rut)
        except FamiliaBlockedError:
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="blocked",
                error="OJV está limitando el acceso; reintentá en unos minutos",
            )
        except Exception as e:
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
