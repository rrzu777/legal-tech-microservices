import logging

from fastapi import APIRouter

from app.auth import verify_api_key
from app.config import get_settings
from app.models import SearchRequest, SearchResponse, CandidateMatch
from app.adapters.http_adapter import OJVHttpAdapter
from app.session import OJVSession
from app.parsers.normalizer import parse_case_identifier, competencia_code, competencia_path
from app.parsers.search_parser import (
    parse_search_results,
    detect_blocked,
    detect_auth_redirect,
    AuthenticationRequired,
)
from app.routes.health import record_successful_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.post("/search", response_model=SearchResponse)
async def search_case(req: SearchRequest, _api_key: str = verify_api_key):
    settings = get_settings()
    adapter = OJVHttpAdapter(settings)
    session = OJVSession(adapter)

    try:
        await session.initialize()

        parsed = parse_case_identifier(req.case_number)
        comp_code = competencia_code(req.competencia)
        comp_path = competencia_path(req.competencia)

        form_data = {
            "g-recaptcha-response-rit": "",
            "action": "validate_captcha_rit",
            "competencia": str(comp_code),
            "conCorte": str(req.corte) if req.competencia == "apelaciones" else "0",
            "conTribunal": "0",
            "conTipoBusApe": "0",
            "radio-groupPenal": "1",
            "conTipoCausa": parsed["tipo"],
            "radio-group": "1",
            "conRolCausa": parsed["numero"],
            "conEraCausa": parsed["anno"],
            "ruc1": "",
            "ruc2": "",
            "rucPen1": "",
            "rucPen2": "",
            "conCaratulado": "",
        }

        html = await session.search(comp_path, form_data)

        if detect_blocked(html):
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=True,
                error="Request blocked by WAF or captcha",
            )

        if detect_auth_redirect(html):
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=True,
                error="Authentication required - OJV session expired",
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
        return SearchResponse(
            found=False, match_count=0, matches=[], blocked=False,
            error=str(e),
        )
    finally:
        await session.close()
