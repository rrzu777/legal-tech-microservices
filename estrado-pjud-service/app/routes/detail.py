import base64
import json
import logging

import httpx
from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.rate_limit import limiter
from app.models import (
    DetailRequest, DetailResponse, CaseMetadata, Movement, Litigante,
)
from app.parsers.detail_parser import parse_detail
from app.metrics import api_metrics
from app.errors import safe_error
from app.alerting import maybe_alert

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
    session = await pool.acquire()

    healthy = True
    try:
        api_metrics.record_request("detail")

        html = await session.detail(comp, req.detail_key)

        if not html or len(html.strip()) < 100:
            healthy = False
            api_metrics.record_blocked("detail")
            await maybe_alert(request)
            logger.warning(
                "Detail blocked for comp=%s — response length=%d, body=%r",
                comp, len(html) if html else 0, (html or "")[:500],
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
        )

    except Exception as e:
        logger.exception("Detail fetch failed")
        healthy = False
        api_metrics.record_error("detail")
        blocked = isinstance(e, (httpx.TimeoutException, httpx.ConnectError))
        if blocked:
            api_metrics.record_blocked("detail")
            await maybe_alert(request)
        return DetailResponse(
            metadata={}, movements=[], litigantes=[], libro=None, blocked=blocked,
            error=safe_error(e),
        )
    finally:
        await pool.release(session, healthy=healthy)
