"""FastAPI fail-closed boundary for proxy cost-control errors."""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.proxy_billing import is_proxy_billing_error
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError

logger = logging.getLogger(__name__)
_PROXY_UNAVAILABLE_DETAIL = "Servicio de sincronizacion temporalmente no disponible"


async def proxy_cost_control_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Persist a safe pause even when a route tracker fails during __aexit__."""
    control = getattr(request.app.state, "proxy_control", None)
    if is_proxy_billing_error(exc) and control is not None:
        await control.trip_billing_exhausted()
    elif isinstance(exc, ProxyUsagePersistenceError) and control is not None:
        await control.pause_telemetry_unavailable()
    elif (
        isinstance(exc, ProxyBudgetExceededError)
        and exc.blocking_scope == "global"
        and control is not None
    ):
        await control.refresh()
    logger.error("Proxy cost control rejected request (%s)", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": _PROXY_UNAVAILABLE_DETAIL})
