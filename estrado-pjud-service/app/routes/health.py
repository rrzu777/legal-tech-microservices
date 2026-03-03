import time

from fastapi import APIRouter

from app.models import HealthResponse

router = APIRouter(prefix="/api/v1", tags=["health"])

_start_time = time.time()
_last_successful_request: float | None = None


def record_successful_request():
    global _last_successful_request
    _last_successful_request = time.time()


@router.get("/health", response_model=HealthResponse)
async def health():
    from datetime import datetime, timezone

    last = None
    if _last_successful_request:
        last = datetime.fromtimestamp(_last_successful_request, tz=timezone.utc).isoformat()

    return HealthResponse(
        status="ok",
        last_successful_request=last,
        uptime_seconds=int(time.time() - _start_time),
    )
