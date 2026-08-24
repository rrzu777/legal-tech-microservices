import asyncio
import logging

import httpx
from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.rate_limit import limiter
from app.catalogs import normalize_catalog_label
from app.matching import (
    build_search_response,
    is_definitive_not_found,
    matches_requested_candidate,
)
from app.models import (
    CandidateMatch,
    DetailResponse,
    CaseMetadata,
    Litigante,
    Movement,
    PenalBookBatchSearchResponse,
    PenalBookBatchSearchRequest,
    PenalBookSafeDetail,
    PenalBookSafeMovement,
    PenalBookVerifiedMatch,
    SearchRequest,
    SearchResponse,
)
from app.my_causes.identity import known_penal_book_code_for_rit
from app.parsers.detail_parser import parse_detail
from app.parsers.form_builder import build_search_form_data
from app.parsers.normalizer import competencia_path, parse_search_identifier, resolve_libro
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.metrics import api_metrics
from app.errors import safe_error
from app.failure_kind import reject_empty_body
from app.pool_guard import acquire_or_alert, classify_and_alert, record_blocked_and_alert
from worker.proxy_usage import DISABLED_PROXY_USAGE

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


async def _search_case(
    req: SearchRequest,
    request: Request,
    *,
    session=None,
) -> SearchResponse:
    pool = request.app.state.session_pool
    # Fuera del `try` de abajo a proposito: ese try devuelve 200 con `error` para
    # TODO lo que falla adentro, asi que un fallo de pool ahi seria invisible.
    # `acquire_or_alert` lo cuenta, alerta y convierte indisponibilidad conocida
    # en 503; un 500 queda reservado para defectos inesperados.
    owns_session = session is None
    if owns_session:
        session = await acquire_or_alert(pool, request, "search")

    healthy = True
    try:
        api_metrics.record_request("search")
        logger.info(
            "PJUD search requested competencia=%s case_type=%s",
            req.competencia,
            req.case_type,
        )

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
        # como fallo de infraestructura. Sin esto, `parse_search_results` de un cuerpo vacio devuelve
        # [] y la app escribe "No encontrada en OJV — revisa el rol".
        if blocked_response:
            healthy = False
            await record_blocked_and_alert(request, "search")
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=True,
                error="Request blocked by WAF or captcha",
                case_type=req.case_type,
                libro_used=None,
                status="pjud_blocked",
            )

        try:
            raw_matches = parse_search_results(html, req.competencia)
        except ValueError:
            if req.contract_version == 2:
                return SearchResponse(
                    found=False, match_count=0, matches=[], blocked=False,
                    error="PJUD search response could not be parsed", case_type=req.case_type,
                    libro_used=None,
                    status="upstream_changed",
                )
            raise
        matches = [CandidateMatch(**m) for m in raw_matches]

        if req.contract_version == 2 and not matches and not is_definitive_not_found(html):
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=False,
                error="PJUD search response could not be parsed", case_type=req.case_type,
                libro_used=None,
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
                    error="PJUD search identity could not be resolved", case_type=req.case_type,
                    libro_used=None,
                    status="upstream_changed",
                )

        api_metrics.record_success("search")

        response = build_search_response(matches, req, libro_used=libro_used)
        response.case_type = req.case_type
        return response

    except Exception as e:
        logger.exception("Search failed")
        healthy = False
        api_metrics.record_error("search")
        if req.contract_version == 2 and isinstance(e, httpx.TimeoutException):
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=False,
                error="PJUD request timed out", case_type=req.case_type, libro_used=None,
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
            case_type=req.case_type,
            libro_used=None,
            status="pjud_blocked" if kind == "ojv" else "not_found",
        )
    finally:
        if owns_session:
            await pool.release(session, healthy=healthy)


def _tribunal_tokens(value: str) -> list[str]:
    aliases = {"JDO": "JUZGADO", "COB": "COBRANZA"}
    return [
        aliases.get(token, token)
        for token in normalize_catalog_label(value).split()
        if token not in {"DE", "DEL", "LA", "EL"}
    ]


def _batch_evidence_compatible(
    candidate: CandidateMatch,
    req: PenalBookBatchSearchRequest,
) -> bool:
    expected = _tribunal_tokens(req.tribunal_label)
    actual = _tribunal_tokens(candidate.tribunal)
    return (
        len(expected) >= 3
        and len(expected) <= len(actual)
        and expected == actual[:len(expected)]
        and normalize_catalog_label(candidate.caratulado)
        == normalize_catalog_label(req.caption)
        and candidate.corte_code is not None
        and candidate.tribunal_code is not None
        and candidate.libro_code in req.books
    )


def _batch_response(
    match_count: int,
    status: str,
    *,
    verified_match: PenalBookVerifiedMatch | None = None,
    detail: PenalBookSafeDetail | None = None,
    error: str | None = None,
    blocked: bool = False,
) -> PenalBookBatchSearchResponse:
    return PenalBookBatchSearchResponse(
        found=verified_match is not None and detail is not None,
        match_count=match_count,
        verified_match=verified_match,
        detail=detail,
        blocked=blocked, error=error, case_type="rit",
        status=status,
    )


def _safe_penal_detail(
    detail: DetailResponse,
    candidate: CandidateMatch,
) -> PenalBookSafeDetail:
    return PenalBookSafeDetail(
        metadata=detail.metadata,
        movements=[PenalBookSafeMovement(
            folio=movement.folio,
            cuaderno=movement.cuaderno,
            etapa=movement.etapa,
            tramite=movement.tramite,
            descripcion=movement.descripcion,
            fecha=movement.fecha,
            foja=movement.foja,
            documento_url=None,
            sala=movement.sala,
            estado=movement.estado,
        ) for movement in detail.movements],
        litigantes=detail.litigantes,
        libro=candidate.libro_code,
    )


def _penal_detail_matches_listing(
    detail: DetailResponse,
    candidate: CandidateMatch,
    req: PenalBookBatchSearchRequest,
) -> bool:
    metadata = detail.metadata
    detail_rol = metadata.rol if isinstance(metadata, CaseMetadata) else str(metadata.get("rol", ""))
    detail_tribunal = metadata.tribunal if isinstance(metadata, CaseMetadata) else str(metadata.get("tribunal", ""))
    detail_libro = (detail.libro or (
        metadata.libro if isinstance(metadata, CaseMetadata) else str(metadata.get("libro", ""))
    ))
    detail_caption = (
        metadata.caratulado if isinstance(metadata, CaseMetadata)
        else str(metadata.get("caratulado", ""))
    )
    observed_prefix = candidate.rol.split("-", 1)[0].strip().upper()
    known_book_code = known_penal_book_code_for_rit(candidate.rol)
    return (
        normalize_catalog_label(detail_rol) == normalize_catalog_label(candidate.rol)
        and normalize_catalog_label(detail_tribunal)
        == normalize_catalog_label(candidate.tribunal)
        and normalize_catalog_label(detail_libro)
        == normalize_catalog_label(observed_prefix)
        and (known_book_code is None or known_book_code == candidate.libro_code)
        and (
            not detail_caption
            or normalize_catalog_label(detail_caption)
            == normalize_catalog_label(req.caption)
        )
    )


async def _fetch_penal_detail(
    session,
    candidate: CandidateMatch,
    request: Request,
) -> DetailResponse:
    proxy_usage = getattr(request.app.state, "proxy_usage", DISABLED_PROXY_USAGE)
    async with proxy_usage.track(operation="detail") as usage:
        html = await session.detail(competencia_path("penal"), candidate.key)
        reject_empty_body(html, "penal batch detail")
        blocked_response = detect_blocked(html)
        if blocked_response:
            usage.status = "blocked"
            usage.error_kind = "ojv"
    if blocked_response:
        return DetailResponse(
            metadata={}, movements=[], litigantes=[], libro=None,
            blocked=True, error="Empty or blocked response from OJV",
        )
    parsed = parse_detail(html)
    metadata = CaseMetadata(**parsed["metadata"]) if parsed["metadata"] else CaseMetadata()
    return DetailResponse(
        metadata=metadata,
        movements=[Movement(**movement) for movement in parsed["movements"]],
        litigantes=[Litigante(**party) for party in parsed["litigantes"]],
        libro=metadata.libro or None,
        blocked=False,
        error=None,
    )


async def _run_penal_book_batch(
    req: PenalBookBatchSearchRequest,
    request: Request,
    *,
    session=None,
) -> PenalBookBatchSearchResponse:
    owns_session = session is None
    pool = request.app.state.session_pool if owns_session else None
    if owns_session:
        session = await acquire_or_alert(pool, request, "penal_book_batch")
    healthy = True
    compatible: dict[tuple[int, int, str], CandidateMatch] = {}
    try:
        for book in req.books:
            branch = await _search_case(SearchRequest(
                contract_version=2,
                competencia="penal",
                case_type="rit",
                case_number=req.case_number,
                libro=book,
                allow_broad=True,
                max_matches=100,
            ), request, session=session)
            if branch.blocked or branch.error or branch.truncated:
                # The nested search owns no release when it shares the batch
                # session, so the outer owner must retire it for every
                # incomplete/blocked response (including a caught timeout).
                healthy = False
                return _batch_response(
                    0, branch.status, error=branch.error, blocked=branch.blocked,
                )
            for candidate in branch.matches:
                if _batch_evidence_compatible(candidate, req):
                    compatible[(
                        candidate.corte_code,
                        candidate.tribunal_code,
                        candidate.libro_code,
                    )] = candidate

        if len(compatible) == 0:
            return _batch_response(0, "not_found")
        if len(compatible) != 1:
            return _batch_response(len(compatible), "needs_disambiguation")

        verified_candidate = next(iter(compatible.values()))
        detail = await _fetch_penal_detail(session, verified_candidate, request)
        if detail.blocked or detail.error:
            healthy = False
            return _batch_response(
                0, "pjud_blocked" if detail.blocked else "upstream_changed",
                error=detail.error, blocked=detail.blocked,
            )
        if not _penal_detail_matches_listing(detail, verified_candidate, req):
            healthy = False
            return _batch_response(
                0, "upstream_changed", error="PJUD detail identity mismatch",
            )
        return _batch_response(
            1,
            "found",
            verified_match=PenalBookVerifiedMatch.model_validate(
                verified_candidate.model_dump(exclude={"key"}),
            ),
            detail=_safe_penal_detail(detail, verified_candidate),
        )
    except BaseException:
        healthy = False
        raise
    finally:
        if owns_session:
            assert pool is not None
            await pool.release(session, healthy=healthy)


@router.post("/search", response_model=SearchResponse)
@limiter.limit("5/minute")
async def search_case(req: SearchRequest, request: Request, _api_key: str = verify_api_key):
    return await _search_case(req, request)


@router.post("/search/penal-books", response_model=PenalBookBatchSearchResponse)
@limiter.limit("1/minute")
async def search_penal_books(
    req: PenalBookBatchSearchRequest,
    request: Request,
    _api_key: str = verify_api_key,
):
    try:
        async with asyncio.timeout(45):
            return await _run_penal_book_batch(req, request)
    except TimeoutError:
        return _batch_response(
            0, "pjud_timeout", error="PJUD batch request timed out",
        )
