import hashlib
import json
import logging

import httpx
from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.catalog_observations import CatalogRefreshIntent
from app.rate_limit import limiter
from app.catalogs import normalize_catalog_label
from app.matching import (
    build_search_response,
    is_definitive_not_found,
    matches_requested_candidate,
)
from app.models import SearchRequest, SearchResponse, CandidateMatch
from app.parsers.form_builder import build_search_form_data
from app.parsers.normalizer import competencia_path, parse_search_identifier, resolve_libro
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.metrics import api_metrics
from app.errors import safe_error
from app.failure_kind import reject_empty_body
from app.pool_guard import acquire_or_alert, classify_and_alert, record_blocked_and_alert
from app.usage_context import current_usage_scope
from worker.proxy_usage import DISABLED_PROXY_USAGE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search"])


class CanonicalCatalogResolutionError(ValueError):
    """A parsed v2 row cannot be tied to one loaded official identity."""


def _catalog_refresh_intents(req: SearchRequest) -> list[CatalogRefreshIntent]:
    """Build only the public catalog slices proven by one canonical request."""
    if req.contract_version != 2 or req.competencia == "suprema" or req.corte is None:
        return []

    parsed = parse_search_identifier(req.case_type, req.case_number)
    anno = int(parsed["anno"]) if parsed.get("anno") else None
    resolved_book = (req.libro or parsed.get("tipo") or "").strip() or None
    canonical_request = {
        "allow_broad": req.allow_broad,
        "anno": anno,
        "case_number": req.case_number.strip().upper(),
        "case_type": req.case_type,
        "competencia": req.competencia,
        "corte": req.corte,
        "libro": resolved_book,
        "search_mode": req.search_mode,
        "tribunal": req.tribunal,
    }
    request_hash = hashlib.sha256(json.dumps(
        canonical_request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    tenant = current_usage_scope()

    intents: list[CatalogRefreshIntent] = []
    if req.tribunal is not None:
        intents.append(CatalogRefreshIntent(
            slice_key=f"tribunals:{req.competencia}:{req.corte}",
            catalog="tribunals",
            competencia=req.competencia,
            corte=req.corte,
            anno=None,
            law_firm_id=tenant["law_firm_id"],
            case_id=tenant["case_id"],
            sync_run_id=tenant["sync_run_id"],
            request_hash=request_hash,
        ))

    uses_book_catalog = (
        req.competencia in {"civil", "laboral", "cobranza"}
        or (req.competencia == "penal" and req.case_type == "rit")
        or (
            req.competencia == "apelaciones"
            and req.search_mode == "appeals_resource"
        )
    )
    if uses_book_catalog and anno is not None:
        intents.append(CatalogRefreshIntent(
            slice_key=f"books:{req.competencia}:{req.corte}:{anno}",
            catalog="books",
            competencia=req.competencia,
            corte=req.corte,
            anno=anno,
            law_firm_id=tenant["law_firm_id"],
            case_id=tenant["case_id"],
            sync_run_id=tenant["sync_run_id"],
            request_hash=request_hash,
        ))
    return intents[:2]


def _enqueue_catalog_refresh_after_release(
    request: Request,
    release_outcome,
    intents: list[CatalogRefreshIntent],
) -> None:
    """Best-effort side effect; it cannot change the interactive response."""
    if getattr(release_outcome, "requeued", False) is not True or not intents:
        return
    queue = getattr(request.app.state, "catalog_refresh_queue", None)
    if queue is None:
        return
    try:
        queue.enqueue_many(intents)
    except Exception:
        logger.exception("Could not enqueue opportunistic catalog refresh")


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
            if match.corte:
                displayed_court_code = catalog_service.resolve_loaded_court(
                    match.corte
                )
                identity = (
                    catalog_service.resolve_loaded_tribunal(
                        req.competencia,
                        match.tribunal,
                        corte=displayed_court_code,
                    )
                    if displayed_court_code is not None
                    else None
                )
            else:
                identity, _used_global_fallback = (
                    _resolve_loaded_tribunal_with_global_fallback(
                        catalog_service, req, match.tribunal,
                    )
                )
                displayed_court_code = identity.court_code if identity else None
            if displayed_court_code is None or identity is None:
                raise CanonicalCatalogResolutionError(
                    "first-instance territorial identity is unresolved"
                )
            match.corte_code = identity.court_code
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
    refresh_intents: list[CatalogRefreshIntent] = []
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

        proxy_usage = getattr(request.app.state, "proxy_usage", DISABLED_PROXY_USAGE)
        async with proxy_usage.track(operation="search") as usage:
            html = await session.search(comp_path, form_data)
            reject_empty_body(html, "search")
            blocked_response = detect_blocked(html)
            if blocked_response:
                usage.status = "blocked"
                usage.error_kind = "ojv"

        # Cuerpo de cero bytes: infra, no bloqueo. Sale por el `except` de abajo
        # como 500. Sin esto, `parse_search_results` de un cuerpo vacio devuelve
        # [] y la app escribe "No encontrada en OJV — revisa el rol".
        if blocked_response:
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
                enrichment_kwargs = {
                    "anno": int(parsed["anno"]) if parsed["anno"] else None,
                    "book_code": (
                        req.libro or libro_used
                        if req.competencia not in {"suprema", "apelaciones"}
                        and req.case_type != "ruc"
                        else None
                    ),
                    "catalog_service": getattr(
                        request.app.state, "catalog_service", None,
                    ),
                }
                if req.allow_broad:
                    exact_matches = [
                        match for match in matches
                        if matches_requested_candidate(match, req)
                    ]
                    resolved_matches = []
                    for match in exact_matches:
                        try:
                            _enrich_v2_candidates(
                                [match], req, **enrichment_kwargs,
                            )
                        except CanonicalCatalogResolutionError:
                            continue
                        resolved_matches.append(match)
                    if exact_matches and not resolved_matches:
                        raise CanonicalCatalogResolutionError(
                            "no exact broad candidate has canonical identity"
                        )
                    if len(resolved_matches) != len(exact_matches):
                        logger.info(
                            "Skipped %d broad candidates without canonical identity",
                            len(exact_matches) - len(resolved_matches),
                        )
                    matches = resolved_matches
                else:
                    exact_matches = [
                        match for match in matches
                        if matches_requested_candidate(match, req)
                    ]
                    if exact_matches:
                        matches = exact_matches
                    _enrich_v2_candidates(
                        matches, req, **enrichment_kwargs,
                    )
            except CanonicalCatalogResolutionError:
                logger.warning("Loaded PJUD catalog could not resolve search identity")
                return SearchResponse(
                    found=False, match_count=0, matches=[], blocked=False,
                    error="PJUD search identity could not be resolved", libro_used=None,
                    status="upstream_changed",
                )

        api_metrics.record_success("search")

        response = build_search_response(matches, req, libro_used=libro_used)
        if response.found:
            refresh_intents = _catalog_refresh_intents(req)
        return response

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
        release_outcome = await pool.release(session, healthy=healthy)
        if healthy:
            _enqueue_catalog_refresh_after_release(
                request,
                release_outcome,
                refresh_intents,
            )
