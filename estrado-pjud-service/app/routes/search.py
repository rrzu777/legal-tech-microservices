import logging

import httpx
from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.rate_limit import limiter
from app.catalogs import CatalogResult, resolve_catalog_code
from app.matching import build_search_response, is_definitive_not_found
from app.models import SearchRequest, SearchResponse, CandidateMatch
from app.parsers.form_builder import build_search_form_data
from app.parsers.normalizer import competencia_path, parse_search_identifier, resolve_libro
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.metrics import api_metrics
from app.errors import safe_error
from app.failure_kind import reject_empty_body
from app.pool_guard import acquire_or_alert, classify_and_alert, record_blocked_and_alert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search"])


async def resolve_tribunal_code(
    catalog_service, request: SearchRequest, tribunal_label: str
) -> int | None:
    """Map a result label only when the official catalog has one exact label."""
    if catalog_service is None or request.corte is None or not tribunal_label.strip():
        return None
    result: CatalogResult = await catalog_service.tribunals(
        request.competencia, request.corte
    )
    return resolve_catalog_code(result.options, tribunal_label)


async def resolve_corte_code(catalog_service, corte_label: str) -> int | None:
    """Map a result court label only when the official courts catalog is unique."""
    if catalog_service is None or not corte_label.strip():
        return None
    result: CatalogResult = await catalog_service.courts()
    return resolve_catalog_code(result.options, corte_label)


@router.post("/search", response_model=SearchResponse)
@limiter.limit("5/minute")
async def search_case(req: SearchRequest, request: Request, _api_key: str = verify_api_key):
    pool = request.app.state.session_pool
    # Fuera del `try` de abajo a proposito: ese try devuelve 200 con `error` para
    # TODO lo que falla adentro, asi que un fallo de pool ahi seria invisible.
    # `acquire_or_alert` lo cuenta, alerta y re-lanza — el 500 es lo unico que le
    # dice a la app que el problema es nuestro y no de la causa.
    session = await acquire_or_alert(pool, request, "search")

    healthy = True
    try:
        api_metrics.record_request("search")

        parsed = parse_search_identifier(req.case_type, req.case_number)
        comp_path = competencia_path(req.competencia)

        form_data = build_search_form_data(
            competencia=req.competencia,
            case_type=req.case_type,
            case_number=req.case_number,
            corte=req.corte if req.contract_version == 2 else (
                req.corte if req.competencia == "apelaciones" else 0
            ),
            tribunal=req.tribunal,
            libro=req.libro,
            search_mode=req.search_mode,
            allow_broad=req.allow_broad,
        )

        libro_used = resolve_libro(req.competencia, parsed["tipo"], req.libro) or None

        html = await session.search(comp_path, form_data)

        # Cuerpo de cero bytes: infra, no bloqueo. Sale por el `except` de abajo
        # como 500. Sin esto, `parse_search_results` de un cuerpo vacio devuelve
        # [] y la app escribe "No encontrada en OJV — revisa el rol".
        reject_empty_body(html, "search")

        if detect_blocked(html):
            healthy = False
            await record_blocked_and_alert(request, "search")
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=True,
                error="Request blocked by WAF or captcha",
                libro_used=None,
                status="pjud_blocked",
            )

        try:
            raw_matches = parse_search_results(html, req.competencia)
        except ValueError:
            if req.contract_version == 2:
                return SearchResponse(
                    found=False, match_count=0, matches=[], blocked=False,
                    error="PJUD search response could not be parsed", libro_used=None,
                    status="upstream_changed",
                )
            raise
        matches = [CandidateMatch(**m) for m in raw_matches]

        if req.contract_version == 2 and not matches and not is_definitive_not_found(html):
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=False,
                error="PJUD search response could not be parsed", libro_used=None,
                status="upstream_changed",
            )

        if req.contract_version == 2 and req.corte is not None:
            resolved_labels: dict[str, int | None] = {}
            catalog_service = getattr(request.app.state, "catalog_service", None)
            if catalog_service is not None:
                court_codes: dict[str, int | None] = {}
                try:
                    if any(match.corte for match in matches):
                        courts: CatalogResult = await catalog_service.courts()
                        for match in matches:
                            if match.corte and match.corte not in court_codes:
                                court_codes[match.corte] = resolve_catalog_code(
                                    courts.options, match.corte
                                )
                            if match.corte:
                                match.corte_code = court_codes[match.corte]
                    if req.tribunal is not None:
                        catalog: CatalogResult = await catalog_service.tribunals(
                            req.competencia, req.corte
                        )
                    else:
                        catalog = None
                except Exception:
                    # Codes influence ranking only.  A catalog outage must not
                    # erase an already parsed PJUD result or turn it into a
                    # false `not_found`; unresolved candidates remain explicit.
                    logger.warning("Tribunal catalog unavailable; returning unranked codes", exc_info=True)
                else:
                    if catalog is not None:
                        for match in matches:
                            if match.tribunal not in resolved_labels:
                                resolved_labels[match.tribunal] = resolve_catalog_code(
                                    catalog.options, match.tribunal
                                )
                            match.tribunal_code = resolved_labels[match.tribunal]

        api_metrics.record_success("search")

        return build_search_response(matches, req, libro_used=libro_used)

    except Exception as e:
        logger.exception("Search failed")
        healthy = False
        api_metrics.record_error("search")
        if req.contract_version == 2 and isinstance(e, httpx.TimeoutException):
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=False,
                error="PJUD request timed out", libro_used=None,
                status="pjud_timeout",
            )
        kind = await classify_and_alert(e, request, "search")

        # Mismo motivo que el `acquire_or_alert` de arriba: una falla NUESTRA no
        # puede salir con 200. Es el 5xx lo que le dice a la app que clasifique
        # esto como infra y no le sume una falla a la causa.
        if kind == "infra" or (req.contract_version == 2 and kind == "case"):
            raise

        return SearchResponse(
            found=False, match_count=0, matches=[], blocked=kind == "ojv",
            error=safe_error(e),
            libro_used=None,
            status="pjud_blocked" if kind == "ojv" else "not_found",
        )
    finally:
        await pool.release(session, healthy=healthy)
