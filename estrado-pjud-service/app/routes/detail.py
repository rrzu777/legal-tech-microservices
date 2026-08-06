import base64
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.auth import verify_api_key
from app.rate_limit import limiter
from app.models import (
    DetailRequest, DetailResponse, CaseMetadata, Movement, Litigante,
)
from app.parsers.detail_parser import parse_detail
from app.parsers.form_builder import build_search_form_data
from app.matching import matches_requested_identifier
from app.parsers.normalizer import competencia_path
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.metrics import api_metrics
from app.errors import safe_error
from app.failure_kind import BlockedPageError, UpstreamChangedError, reject_empty_body
from app.pool_guard import acquire_or_alert, classify_and_alert, record_blocked_and_alert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["detail"])


_CODE_MAP = {1: "suprema", 2: "apelaciones", 3: "civil", 4: "laboral", 5: "penal", 6: "cobranza"}
_VALID_COMPS = set(_CODE_MAP.values())


def _guess_competencia_from_jwt(jwt: str) -> str | None:
    """Try to extract competencia from the JWT payload. Returns None if not found."""
    try:
        payload = jwt.split(".")[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        logger.debug("JWT payload keys: %s", list(data.keys()))
        comp = data.get("competencia", "").lower() if isinstance(data.get("competencia"), str) else ""
        if comp in _VALID_COMPS:
            return comp
        code = data.get("codCompetencia")
        if isinstance(code, int) and code in _CODE_MAP:
            return _CODE_MAP[code]
        # Try numeric string
        if isinstance(code, str) and code.isdigit() and int(code) in _CODE_MAP:
            return _CODE_MAP[int(code)]
        logger.warning("Could not extract competencia from JWT payload: %s", {k: v for k, v in data.items() if k in ("competencia", "codCompetencia", "codTipoCompetencia")})
    except Exception:
        logger.warning("Failed to decode JWT for competencia extraction")
    return None


async def _search_for_fresh_jwt(session, comp: str, req: DetailRequest) -> str | None:
    """Perform a search on the SAME session to get a fresh JWT with session affinity.

    This eliminates cross-case contamination caused by JWT + CSRF coming from
    different sessions when the session pool reuses sessions.

    Returns the fresh JWT key, or None if the search fails to find a match.
    """
    comp_path = competencia_path(comp)

    form_data = build_search_form_data(
        competencia=comp,
        case_type=req.case_type or "rol",
        case_number=req.case_number,
        corte=req.corte if req.contract_version == 2 else (
            req.corte if comp == "apelaciones" else 0
        ),
        tribunal=req.tribunal,
        libro=req.libro,
        search_mode=req.search_mode,
        allow_broad=req.allow_broad,
    )

    html = await session.search(comp_path, form_data)

    reject_empty_body(html, "detail affinity search")
    if detect_blocked(html):
        raise BlockedPageError("detail affinity search returned WAF or captcha")

    try:
        matches = parse_search_results(html, comp)
    except ValueError as exc:
        raise UpstreamChangedError("detail affinity search parser rejected PJUD response") from exc

    if not matches:
        raise UpstreamChangedError("detail affinity search returned non-empty unparseable PJUD response")

    exact = [
        match for match in matches
        if matches_requested_identifier(str(match.get("rol", "")), req)
    ]

    # The JWT `data` field is case-specific and stable across sessions.  It is
    # combined with the requested official identifier, so a stale caller JWT
    # cannot select another row from a broad result set.
    caller_data = _extract_jwt_data(req.detail_key)
    if caller_data:
        correlated = [
            match for match in exact
            if _extract_jwt_data(match["key"]) == caller_data
        ]
        if len(correlated) == 1:
            logger.info("Session-affinity search: correlated fresh JWT among %d results", len(matches))
            return correlated[0]["key"]

    # The only safe first-match case is unique by the official identifier.
    if len(exact) == 1:
        logger.info("Session-affinity search: unique exact official identifier")
        return exact[0]["key"]

    logger.error(
        "Session-affinity search: %d matches, could not correlate requested candidate",
        len(matches),
    )
    return None


def _extract_jwt_data(jwt: str) -> str | None:
    """Extract the 'data' field from a PJUD JWT payload for correlation.

    Normalizes to string so dict/list values compare reliably.
    """
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded).get("data")
        if data is None:
            return None
        return json.dumps(data, sort_keys=True) if isinstance(data, (dict, list)) else str(data)
    except Exception:
        return None


@router.post("/detail", response_model=DetailResponse)
@limiter.limit("5/minute")
async def case_detail(req: DetailRequest, request: Request, _api_key: str = verify_api_key):
    if req.competencia:
        comp = req.competencia
    else:
        comp = _guess_competencia_from_jwt(req.detail_key)
        if not comp:
            logger.error("competencia not provided and could not be inferred from JWT")
            return DetailResponse(
                metadata={}, movements=[], litigantes=[], libro=None, blocked=True,
                error="competencia is required (could not infer from JWT)",
            )
        logger.info("Inferred competencia=%s from JWT", comp)

    pool = request.app.state.session_pool
    # Mismo motivo que en `search`: el try de abajo devuelve 200 con `error` para
    # todo lo que pasa adentro, asi que un fallo de pool ahi seria invisible.
    session = await acquire_or_alert(pool, request, "detail")

    healthy = True
    try:
        api_metrics.record_request("detail")

        # Session-affinity: if search params are provided, search on the SAME
        # session first so the JWT and CSRF share the same PJUD server context.
        detail_key = req.detail_key
        if req.case_number:
            fresh_key = await _search_for_fresh_jwt(session, comp, req)
            if fresh_key:
                detail_key = fresh_key
            else:
                raise HTTPException(
                    status_code=409,
                    detail="Could not establish PJUD session affinity for the requested candidate",
                )

        html = await session.detail(competencia_path(comp), detail_key)

        # Cuerpo de cero bytes: infra, no bloqueo. Una pagina contentless de ~39
        # bytes SI es un soft-block de F5 (esta medido, y por eso el `len < 100`
        # de abajo sigue contando como bloqueo), pero cero bytes es la respuesta
        # que no llego. Sale por el `except` como 500 para que la app lo lea como
        # nuestro.
        reject_empty_body(html, "detail")

        # `detect_blocked` ademas del largo, igual que hace `search` y que hace
        # `detail_pjud_via_session` en el worker. Esta ruta miraba SOLO el largo,
        # asi que un challenge F5 completo —varios KB de pagina con `bobcmn`—
        # pasaba de largo, `parse_detail` no encontraba nada y la respuesta salia
        # 200 con `blocked=False` y la causa vacia: el abogado veia un expediente
        # sin movimientos en vez de un aviso de bloqueo. El worker tenia el test
        # de esto (`test_detail_detects_f5_challenge`) y la ruta no.
        if len(html.strip()) < 100 or detect_blocked(html):
            healthy = False
            await record_blocked_and_alert(request, "detail")
            logger.warning(
                "Detail blocked for comp=%s — response length=%d, body=%r",
                comp, len(html), html[:500],
            )
            return DetailResponse(
                metadata={}, movements=[], litigantes=[], libro=None, blocked=True,
                error="Empty or blocked response from OJV",
            )

        parsed = parse_detail(html)

        metadata = CaseMetadata(**parsed["metadata"]) if parsed["metadata"] else CaseMetadata()
        movements = [Movement(**m) for m in parsed["movements"]]
        litigantes = [Litigante(**l) for l in parsed["litigantes"]]

        api_metrics.record_success("detail")

        return DetailResponse(
            metadata=metadata,
            movements=movements,
            litigantes=litigantes,
            libro=metadata.libro or None,
            blocked=False,
            error=None,
            ebook_token=parsed.get("ebook_token", ""),
            certificado_disponible=parsed.get("certificado_disponible", False),
            suprema_docs=parsed.get("suprema_docs", []),
            exhortos=parsed.get("exhortos", []),
            incompetencia=parsed.get("incompetencia", []),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Detail fetch failed")
        healthy = False
        api_metrics.record_error("detail")
        kind = await classify_and_alert(e, request, "detail")

        # Mismo motivo que en `search`: una falla NUESTRA no puede salir con 200.
        if kind == "infra":
            raise

        return DetailResponse(
            metadata={}, movements=[], litigantes=[], libro=None, blocked=kind == "ojv",
            error=safe_error(e),
        )
    finally:
        await pool.release(session, healthy=healthy)
