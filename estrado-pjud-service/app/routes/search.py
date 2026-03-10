import logging

from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.models import SearchRequest, SearchResponse, CandidateMatch
from app.parsers.normalizer import parse_case_identifier, competencia_code, competencia_path
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.routes.health import record_successful_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.post("/search", response_model=SearchResponse)
async def search_case(req: SearchRequest, request: Request, _api_key: str = verify_api_key):
    pool = request.app.state.session_pool
    session = await pool.acquire()

    try:
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
            "radio-group": "1",
            "conRolCausa": parsed["numero"],
            "conEraCausa": parsed["anno"],
            "ruc1": "",
            "ruc2": "",
            "rucPen1": "",
            "rucPen2": "",
            "conCaratulado": "",
        }

        if req.competencia == "suprema":
            form_data["conTipoBus"] = "0"
        elif req.competencia == "penal":
            # Penal uses RIT/RUC instead of ROL.  radio-groupPenal=1 selects
            # RIT mode (tipo + numero + anno).  RUC search would use
            # radio-groupPenal=2 with rucPen1/rucPen2 fields instead.
            form_data["radio-groupPenal"] = "1"  # RIT mode
            form_data["conTipoCausa"] = parsed["tipo"]
        else:
            form_data["conTipoCausa"] = parsed["tipo"]

        html = await session.search(comp_path, form_data)

        if detect_blocked(html):
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
        return SearchResponse(
            found=False, match_count=0, matches=[], blocked=False,
            error=str(e),
        )
    finally:
        await pool.release(session)
