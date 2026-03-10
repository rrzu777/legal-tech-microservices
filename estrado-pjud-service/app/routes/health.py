from fastapi import APIRouter

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
        status="ok",
        last_successful_request=last,
        **snapshot,
    )
