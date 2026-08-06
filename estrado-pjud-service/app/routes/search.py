import logging

import httpx
from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.rate_limit import limiter
from app.catalogs import normalize_catalog_label
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


class CanonicalCatalogResolutionError(ValueError):
    """A parsed v2 row cannot be tied to one loaded official identity."""


def _resolve_loaded_tribunal_with_global_fallback(
    catalog_service, req: SearchRequest, tribunal_label: str
):
    """Prefer the requested court, then identify a unique out-of-court row."""
    identity = catalog_service.resolve_loaded_tribunal(
        req.competencia, tribunal_label, corte=req.corte,
    )
    if identity is not None or req.corte is None:
        return identity, False
    return (
        catalog_service.resolve_loaded_tribunal(req.competencia, tribunal_label),
        True,
    )


def _enrich_v2_candidates(
    matches: list[CandidateMatch],
    req: SearchRequest,
    *,
    anno: int | None,
    book_code: str | None,
    catalog_service,
) -> None:
    """Mutate parsed candidates with canonical snapshot/cache identity only."""
    if req.competencia == "suprema":
        for match in matches:
            match.corte_code = None
            match.tribunal_code = None
        return
    if catalog_service is None:
        raise CanonicalCatalogResolutionError("catalog service is unavailable")

    if req.competencia == "apelaciones":
        if req.search_mode == "appeals_resource":
            for match in matches:
                court_code = catalog_service.resolve_loaded_court(match.corte or "")
                if court_code is None or match.libro_code is None:
                    raise CanonicalCatalogResolutionError(
                        "appeal court or book is unresolved"
                    )
                match.corte_code = court_code
                match.tribunal_code = None
            return

        for match in matches:
            displayed_court_code = (
                catalog_service.resolve_loaded_court(match.corte)
                if match.corte
                else None
            )
            identity, used_global_fallback = (
                _resolve_loaded_tribunal_with_global_fallback(
                    catalog_service, req, match.tribunal,
                )
            )
            if (match.corte and displayed_court_code is None) or identity is None:
                raise CanonicalCatalogResolutionError(
                    "first-instance territorial identity is unresolved"
                )
            match.corte_code = (
                identity.court_code
                if used_global_fallback
                else displayed_court_code or identity.court_code
            )
            match.tribunal_code = identity.tribunal_code
        return

    for match in matches:
        identity, _used_global_fallback = (
            _resolve_loaded_tribunal_with_global_fallback(
                catalog_service, req, match.tribunal,
            )
        )
        if identity is None:
            raise CanonicalCatalogResolutionError("tribunal identity is unresolved")
        match.corte_code = identity.court_code
        match.tribunal_code = identity.tribunal_code

        if book_code is None:
            continue
        if anno is None:
            raise CanonicalCatalogResolutionError("book year is unavailable")
        official_book = catalog_service.resolve_loaded_book(
            req.competencia, book_code, anno, corte=identity.court_code,
        )
        if official_book is None:
            raise CanonicalCatalogResolutionError("book identity is unresolved")
        if match.libro_code is None and match.libro is None:
            match.libro_code = official_book["code"]
            match.libro = official_book["label"]
        elif (
            match.libro_code is None
            and normalize_catalog_label(match.libro or "")
            == normalize_catalog_label(official_book["label"])
        ):
            match.libro_code = official_book["code"]
        elif match.libro_code == official_book["code"] and match.libro is None:
            match.libro = official_book["label"]


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

        if req.contract_version == 2:
            try:
                _enrich_v2_candidates(
                    matches,
                    req,
                    anno=int(parsed["anno"]) if parsed["anno"] else None,
                    book_code=(
                        req.libro or libro_used
                        if req.competencia not in {"suprema", "apelaciones"}
                        and req.case_type != "ruc"
                        else None
                    ),
                    catalog_service=getattr(request.app.state, "catalog_service", None),
                )
            except CanonicalCatalogResolutionError:
                logger.warning("Loaded PJUD catalog could not resolve search identity")
                return SearchResponse(
                    found=False, match_count=0, matches=[], blocked=False,
                    error="PJUD search identity could not be resolved", libro_used=None,
                    status="upstream_changed",
                )

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
