"""Las guardias que impiden que una falla NUESTRA salga muda o disfrazada.

Dos, y las dos con el mismo enemigo: una ruta que contesta 200 con `blocked` o
`found=False` cuando el que se cayó es este servicio. La app no tiene forma de
distinguir eso de una respuesta real de OJV.

`SessionPool.acquire()` puede lanzar. Hasta hace poco lo hacía por accidente:
sin bundle F5 caía a sin-proxy/sin-cookies y el `initialize()` contra OJV
reventaba a veces. Hoy lanza a propósito —`NoUsableBundleError`— porque ese
fallback egresaba por la IP del datacenter, que F5 tiene soft-blockeada, y las
veces que NO reventaba eran peores: OJV contesta 200 con una página de challenge
y la ruta la reportaba como bloqueo de OJV. Las fallas operacionales tipadas
salen como 503 seguro —la app lo clasifica como `PjudInfraError` y NO le suma
fallas a la causa—, pero antes salían sin dejar rastro:

- `api_metrics.record_request()` se llamaba DESPUÉS del acquire, así que el
  contador quedaba en cero;
- `maybe_alert()` mira el ratio de bloqueos y su primera guardia es
  `total_requests == 0`, o sea que ese cero la hacía salir antes de mirar nada.

Antes de esta frontera tipada, el resultado medido el 31 de julio de 2026 fue
una instancia que llevaba 3 días y 18 horas devolviendo 500 a todas las
consultas y `/api/v1/health` respondía
`{"status": "ok", "total_requests": 0, "total_errors": 0, "blocked_rate": 0.0}`.
Un servicio totalmente caído indistinguible de uno ocioso. Nos enteramos porque
un tester mandó una captura de la pantalla de una causa.

Este módulo cierra las dos: cuenta el fallo en una métrica propia y manda una
alerta por el HECHO (no por la proporción), reserva 500 para defectos
inesperados y entrega 503 sólo para indisponibilidad operacional conocida.
"""

import logging

from fastapi import HTTPException, Request

from app.alerting import maybe_alert, maybe_alert_event
from app.failure_kind import FailureKind, PoolUnavailableError, classify_exception
from app.metrics import api_metrics
from app.proxy_billing import is_proxy_billing_error
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.session_pool import ProxyTrafficDisabledError

logger = logging.getLogger(__name__)

POOL_UNAVAILABLE_EVENT = "pool_unavailable"
INFRA_FAILURE_EVENT = "infra_failure"
PUBLIC_POOL_UNAVAILABLE_DETAIL = "Servicio de sincronizacion temporalmente no disponible"


async def _trace_pool_failure(request: Request, endpoint: str, failure_code: str) -> None:
    """Cuenta el fallo de pool y avisa. El rastro, en un solo lugar.

    Las dos formas de quedarse sin con que salir a la calle —`acquire()` que
    revienta y `pick_familia_bundle()` que devuelve None— son el mismo hecho de
    ops, y por eso comparten métrica, alerta y respuesta 503 segura.
    """
    api_metrics.record_pool_failure(endpoint)
    logger.error("Pool sin sesion disponible para %s: pool_failure=%s", endpoint, failure_code)
    await maybe_alert_event(
        request,
        POOL_UNAVAILABLE_EVENT,
        f"{endpoint}: pool_failure={failure_code}",
    )


async def _preserve_proxy_control_transition(error: Exception, request: Request) -> None:
    """Keep the paid-traffic control transitions outside the 503 translation."""
    control = getattr(request.app.state, "proxy_control", None)
    if isinstance(error, ProxyUsagePersistenceError) and control is not None:
        await control.pause_telemetry_unavailable()
    elif isinstance(error, ProxyBudgetExceededError) and control is not None:
        await control.refresh()


def _safe_pool_failure_code(error: Exception) -> str:
    if isinstance(error, PoolUnavailableError):
        return error.code
    if isinstance(error, ProxyTrafficDisabledError):
        return "proxy_traffic_disabled"
    if isinstance(error, ProxyBudgetExceededError):
        return "proxy_budget_exhausted"
    if isinstance(error, ProxyUsagePersistenceError):
        return "telemetry_unavailable"
    return "unexpected_exception"


def _is_known_operational_unavailability(error: Exception) -> bool:
    return isinstance(
        error,
        (
            PoolUnavailableError,
            ProxyTrafficDisabledError,
            ProxyBudgetExceededError,
            ProxyUsagePersistenceError,
        ),
    )


async def record_blocked_and_alert(request: Request, endpoint: str) -> None:
    """OJV nos corto: contalo y toca el alerter.

    El par estaba escrito en cuatro lugares —las dos rutas al detectar el
    challenge, el `except` clasificado, y la ruta de Familia—, que es como una
    de las cuatro se olvida de la mitad. De hecho `familia.py` no contaba NADA
    hasta esta PR.
    """
    api_metrics.record_blocked(endpoint)
    await maybe_alert(request)


async def acquire_or_alert(pool, request: Request, endpoint: str):
    """Devuelve una sesión del pool; si no puede, deja rastro y re-lanza.

    Re-lanza a propósito y no devuelve una respuesta `blocked=True`: eso haría
    que la app tratara una caída NUESTRA como un bloqueo de OJV, que es
    exactamente la atribución falsa que este trabajo vino a arreglar. El 5xx es
    lo que hace que la app la clasifique bien.
    """
    try:
        return await pool.acquire()
    except Exception as error:
        await _preserve_proxy_control_transition(error, request)
        await _trace_pool_failure(request, endpoint, _safe_pool_failure_code(error))
        if _is_known_operational_unavailability(error):
            raise HTTPException(
                status_code=503,
                detail=PUBLIC_POOL_UNAVAILABLE_DETAIL,
            ) from error
        raise


async def familia_bundle_or_alert(pool, request: Request):
    """El bundle F5 para Familia; recupera on-demand o responde 503.

    `acquire_familia_bundle()` intenta el minteo interactivo protegido cuando
    el worker todavía no minteó ningún slot o cuando todos quedaron obsoletos.
    Sólo si esa recuperación no entrega un bundle se conserva el 503 que evita
    atribuir nuestra falla a un bloqueo real de OJV.

    El 503 es lo que hace que la app la clasifique como infra. Y va con la misma
    métrica y la misma alerta que `acquire_or_alert`, porque es el mismo hecho
    contado desde otra ruta: el pool no tiene con qué salir a la calle.
    """
    try:
        bundle = await pool.acquire_familia_bundle()
    except Exception as error:
        await _preserve_proxy_control_transition(error, request)
        await _trace_pool_failure(request, "familia", _safe_pool_failure_code(error))
        if _is_known_operational_unavailability(error):
            raise HTTPException(
                status_code=503,
                detail=PUBLIC_POOL_UNAVAILABLE_DETAIL,
            ) from error
        raise

    if bundle is not None:
        return bundle

    await _trace_pool_failure(request, "familia", "mint_exhausted")
    raise HTTPException(
        status_code=503,
        detail=PUBLIC_POOL_UNAVAILABLE_DETAIL,
    )


async def classify_and_alert(e: Exception, request: Request, endpoint: str) -> FailureKind:
    """Clasifica la excepción de una ruta y deja el rastro que le corresponde.

    Compartida por `search` y `detail` porque el `except` de las dos era el mismo
    y estaba escrito dos veces — y las dos veces mal, con el mismo
    `isinstance(e, (TimeoutException, ConnectError))`. Esa lista dejaba afuera
    `ProxyError`, `ReadError` y `RemoteProtocolError`, que es justo lo que
    produce un pool de IPs residenciales, y también `HTTPStatusError`. Todo eso
    salía con `blocked=False` y `found=False`, o sea 200, o sea que la app le
    escribía al abogado "No encontrada en OJV — revisa el rol y el tribunal"
    cuando la causa existía perfectamente y el que estaba roto era el proxy.

    No devuelve una respuesta: devuelve la clasificación y deja que cada ruta
    arme su propio modelo. Lo que sí centraliza es el rastro —métrica y alerta—,
    que es lo que se olvida.
    """
    if is_proxy_billing_error(e):
        control = getattr(request.app.state, "proxy_control", None)
        if control is not None:
            await control.trip_billing_exhausted()
    elif isinstance(e, ProxyUsagePersistenceError):
        control = getattr(request.app.state, "proxy_control", None)
        if control is not None:
            await control.pause_telemetry_unavailable()
    elif isinstance(e, ProxyBudgetExceededError):
        control = getattr(request.app.state, "proxy_control", None)
        if control is not None and e.blocking_scope == "global":
            await control.refresh()

    kind = classify_exception(e)

    if kind == "infra":
        await maybe_alert_event(
            request,
            INFRA_FAILURE_EVENT,
            f"{endpoint}: fallo de nuestro lado hablando con OJV "
            f"({type(e).__name__}: {e}). Revisar proxy residencial / bundles F5.",
        )
    elif kind == "ojv":
        await record_blocked_and_alert(request, endpoint)

    return kind
