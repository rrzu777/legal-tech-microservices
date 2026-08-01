from fastapi import APIRouter

from app.config import get_settings
from app.metrics import api_metrics
from app.models import HealthResponse

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health():
    from datetime import datetime, timezone

    last = None
    last_ts = api_metrics.last_successful_request
    if last_ts:
        last = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()

    snapshot = api_metrics.snapshot()

    return HealthResponse(
        # El mismo umbral que le pasa `main.py` al alerter de Telegram: si el
        # panel y la alerta usaran dos numeros, ops leeria dos respuestas
        # distintas a la misma pregunta. El porque del resto esta en
        # `APIMetrics.status`.
        status=api_metrics.status(get_settings().TELEGRAM_BLOCKED_RATE_THRESHOLD),
        last_successful_request=last,
        **snapshot,
    )
