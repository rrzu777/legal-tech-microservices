from fastapi import APIRouter, Request

from app.config import get_settings
from app.metrics import api_metrics
from app.models import HealthResponse
from app.ojv.private_telemetry import private_operational_metrics

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    from datetime import datetime, timezone

    last = None
    last_ts = api_metrics.last_successful_request
    if last_ts:
        last = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()

    snapshot = api_metrics.snapshot()
    control_required = bool(getattr(request.app.state, "proxy_control_required", False))
    control = getattr(request.app.state, "proxy_control", None)
    control_snapshot = await control.refresh() if control_required and control else None
    derived_status = api_metrics.status(get_settings().TELEGRAM_BLOCKED_RATE_THRESHOLD)
    if control_required and (control_snapshot is None or not control_snapshot.allowed):
        derived_status = "down"

    return HealthResponse(
        # El mismo umbral que le pasa `main.py` al alerter de Telegram: si el
        # panel y la alerta usaran dos numeros, ops leeria dos respuestas
        # distintas a la misma pregunta. El porque del resto esta en
        # `APIMetrics.status`.
        status=derived_status,
        last_successful_request=last,
        pjud_available=not control_required or bool(control_snapshot and control_snapshot.allowed),
        private_sync=private_operational_metrics.snapshot(),
        **snapshot,
    )
