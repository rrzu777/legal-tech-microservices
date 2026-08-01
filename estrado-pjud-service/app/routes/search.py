import logging

from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.rate_limit import limiter
from app.models import SearchRequest, SearchResponse, CandidateMatch
from app.parsers.form_builder import build_search_form_data
from app.parsers.normalizer import parse_case_identifier, competencia_path, resolve_libro
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.metrics import api_metrics
from app.errors import safe_error
from app.failure_kind import reject_empty_body
from app.pool_guard import acquire_or_alert, classify_and_alert, record_blocked_and_alert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search"])


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
        kind = await classify_and_alert(e, request, "search")

        # Mismo motivo que el `acquire_or_alert` de arriba: una falla NUESTRA no
        # puede salir con 200. Es el 5xx lo que le dice a la app que clasifique
        # esto como infra y no le sume una falla a la causa.
        if kind == "infra":
            raise

        return SearchResponse(
            found=False, match_count=0, matches=[], blocked=kind == "ojv",
            error=safe_error(e),
            libro_used=None,
        )
    finally:
        await pool.release(session, healthy=healthy)
