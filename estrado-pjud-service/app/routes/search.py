import logging

import httpx
from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.rate_limit import limiter
from app.models import SearchRequest, SearchResponse, CandidateMatch
from app.parsers.form_builder import build_search_form_data
from app.parsers.normalizer import parse_case_identifier, competencia_path, resolve_libro
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.metrics import api_metrics
from app.errors import safe_error
from app.alerting import maybe_alert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.post("/search", response_model=SearchResponse)
@limiter.limit("5/minute")
async def search_case(req: SearchRequest, request: Request, _api_key: str = verify_api_key):
    pool = request.app.state.session_pool
    session = await pool.acquire()

    healthy = True
    try:
        api_metrics.record_request("search")

        parsed = parse_case_identifier(req.case_number)
        comp_path = competencia_path(req.competencia)

        form_data = build_search_form_data(
            competencia=req.competencia,
            tipo=parsed["tipo"],
            numero=parsed["numero"],
            anno=parsed["anno"],
            corte=req.corte if req.competencia == "apelaciones" else 0,
            libro=req.libro,
        )

        libro_used = resolve_libro(req.competencia, parsed["tipo"], req.libro) or None

        html = await session.search(comp_path, form_data)

        if detect_blocked(html):
            healthy = False
            api_metrics.record_blocked("search")
            await maybe_alert(request)
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=True,
                error="Request blocked by WAF or captcha",
                libro_used=None,
            )

        raw_matches = parse_search_results(html, req.competencia)
        matches = [CandidateMatch(**m) for m in raw_matches]

        api_metrics.record_success("search")

        return SearchResponse(
            found=len(matches) > 0,
            match_count=len(matches),
            matches=matches,
            blocked=False,
            error=None,
            libro_used=libro_used,
        )

    except Exception as e:
        logger.exception("Search failed")
        healthy = False
        api_metrics.record_error("search")
        blocked = isinstance(e, (httpx.TimeoutException, httpx.ConnectError))
        if blocked:
            api_metrics.record_blocked("search")
            await maybe_alert(request)
        return SearchResponse(
            found=False, match_count=0, matches=[], blocked=blocked,
            error=safe_error(e),
            libro_used=None,
        )
    finally:
        await pool.release(session, healthy=healthy)
