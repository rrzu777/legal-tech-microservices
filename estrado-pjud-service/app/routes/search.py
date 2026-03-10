import logging

import httpx
from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.models import SearchRequest, SearchResponse, CandidateMatch
from app.parsers.form_builder import build_search_form_data
from app.parsers.normalizer import parse_case_identifier, competencia_path
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.routes.health import record_successful_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.post("/search", response_model=SearchResponse)
async def search_case(req: SearchRequest, request: Request, _api_key: str = verify_api_key):
    pool = request.app.state.session_pool
    session = await pool.acquire()

    healthy = True
    try:
        parsed = parse_case_identifier(req.case_number)
        comp_path = competencia_path(req.competencia)

        form_data = build_search_form_data(
            competencia=req.competencia,
            tipo=parsed["tipo"],
            numero=parsed["numero"],
            anno=parsed["anno"],
            corte=req.corte if req.competencia == "apelaciones" else 0,
        )

        html = await session.search(comp_path, form_data)

        if detect_blocked(html):
            healthy = False
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=True,
                error="Request blocked by WAF or captcha",
            )

        raw_matches = parse_search_results(html, req.competencia)
        matches = [CandidateMatch(**m) for m in raw_matches]

        record_successful_request()

        return SearchResponse(
            found=len(matches) > 0,
            match_count=len(matches),
            matches=matches,
            blocked=False,
            error=None,
        )

    except Exception as e:
        logger.exception("Search failed")
        healthy = False
        return SearchResponse(
            found=False, match_count=0, matches=[], blocked=isinstance(e, (httpx.TimeoutException, httpx.ConnectError)),
            error=str(e),
        )
    finally:
        await pool.release(session, healthy=healthy)
