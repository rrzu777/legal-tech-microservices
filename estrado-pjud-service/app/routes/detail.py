import base64
import json
import logging

from fastapi import APIRouter

from app.auth import verify_api_key
from app.config import get_settings
from app.models import (
    DetailRequest, DetailResponse, CaseMetadata, Movement, Litigante,
)
from app.adapters.http_adapter import OJVHttpAdapter
from app.session import OJVSession
from app.parsers.detail_parser import parse_detail
from app.routes.health import record_successful_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["detail"])


def _guess_competencia_from_jwt(jwt: str) -> str:
    """Try to extract competencia from the JWT payload. Fallback to 'civil'."""
    try:
        payload = jwt.split(".")[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        comp = data.get("competencia", "").lower()
        if comp in ("civil", "laboral", "cobranza"):
            return comp
        code = data.get("codCompetencia") or data.get("competencia")
        code_map = {3: "civil", 4: "laboral", 6: "cobranza"}
        if isinstance(code, int) and code in code_map:
            return code_map[code]
    except Exception:
        pass
    return "civil"


@router.post("/detail", response_model=DetailResponse)
async def case_detail(req: DetailRequest, _api_key: str = verify_api_key):
    settings = get_settings()
    adapter = OJVHttpAdapter(settings)
    session = OJVSession(adapter)

    try:
        await session.initialize()

        comp = _guess_competencia_from_jwt(req.detail_key)

        html = await session.detail(comp, req.detail_key)

        if not html or len(html.strip()) < 100:
            return DetailResponse(
                metadata={}, movements=[], litigantes=[], blocked=True,
                error="Empty or blocked response from OJV",
            )

        parsed = parse_detail(html)

        metadata = CaseMetadata(**parsed["metadata"]) if parsed["metadata"] else CaseMetadata()
        movements = [Movement(**m) for m in parsed["movements"]]
        litigantes = [Litigante(**l) for l in parsed["litigantes"]]

        record_successful_request()

        return DetailResponse(
            metadata=metadata,
            movements=movements,
            litigantes=litigantes,
            blocked=False,
            error=None,
        )

    except Exception as e:
        logger.exception("Detail fetch failed")
        return DetailResponse(
            metadata={}, movements=[], litigantes=[], blocked=True,
            error=str(e),
        )
    finally:
        await session.close()
