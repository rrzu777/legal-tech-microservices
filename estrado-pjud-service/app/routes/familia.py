"""POST /api/v1/familia/sync — sync Familia cases for an authenticated user."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.auth import verify_api_key
from app.errors import safe_error
from app.familia.auth import FamiliaAuthSession, InvalidCredentialsError, SessionError
from app.familia.models import FamiliaSyncRequest, FamiliaSyncResponse
from app.familia.parser import parse_familia_results

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/familia", tags=["familia"])


@router.post("/sync", response_model=FamiliaSyncResponse)
async def familia_sync(
    req: FamiliaSyncRequest,
    _api_key: str = verify_api_key,
) -> FamiliaSyncResponse:
    """Login to OJV with user credentials and return their Familia cases.

    auth_type:
      - "clave_unica"  (default) — OAuth2 flow via CU
      - "clave_pj"               — Direct POST with Clave Poder Judicial

    If cases[] is provided, each RIT is queried individually.
    If cases[] is empty, returns all cases for the RUT.
    """
    async with FamiliaAuthSession() as session:
        # Step 1: authenticate
        try:
            await session.login(req.rut, req.password, req.auth_type)
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

        # Step 2: query — either per-RIT or all cases at once
        all_casos = []
        error_code = None

        if req.cases:
            for case_filter in req.cases:
                try:
                    html = await session.search_familia(
                        rut=req.rut,
                        rit=case_filter.rit,
                        year=case_filter.year,
                    )
                    casos, err = parse_familia_results(html)
                    if err and err != "no_cases":
                        logger.warning(
                            "familia_sync: parse error for RIT %s-%s: %s",
                            case_filter.rit, case_filter.year, err,
                        )
                    all_casos.extend(casos)
                except Exception as e:
                    logger.warning("familia_sync: error querying RIT %s: %s", case_filter.rit, safe_error(e))
        else:
            try:
                html = await session.search_familia(rut=req.rut)
                all_casos, error_code = parse_familia_results(html)
            except Exception as e:
                logger.exception("familia_sync: unexpected error querying Familia")
                return FamiliaSyncResponse(
                    ok=False, casos=[],
                    error_code="session_error",
                    error=safe_error(e),
                )

    if error_code == "no_cases" and not all_casos:
        return FamiliaSyncResponse(
            ok=True, casos=[],
            error_code="no_cases",
            error=None,
        )

    if error_code == "parse_error":
        return FamiliaSyncResponse(
            ok=False, casos=[],
            error_code="parse_error",
            error="No se pudo interpretar la respuesta de OJV",
        )

    return FamiliaSyncResponse(ok=True, casos=all_casos)
