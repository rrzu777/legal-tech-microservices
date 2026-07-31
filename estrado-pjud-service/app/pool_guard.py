"""Conseguir una sesión del pool sin que el fallo salga mudo.

`SessionPool.acquire()` puede lanzar: cuando no hay bundle F5 en el store cae a
sin-proxy/sin-cookies y el `initialize()` contra OJV revienta. En las rutas esa
excepción sale como 500, que es el contrato correcto —la app lo clasifica como
`PjudInfraError` y NO le suma fallas a la causa—, pero salía sin dejar rastro:

- `api_metrics.record_request()` se llamaba DESPUÉS del acquire, así que el
  contador quedaba en cero;
- `maybe_alert()` mira el ratio de bloqueos y su primera guardia es
  `total_requests == 0`, o sea que ese cero la hacía salir antes de mirar nada.

El resultado medido el 31 de julio de 2026: la instancia llevaba 3 días y 18
horas devolviendo 500 a todas las consultas y `/api/v1/health` respondía
`{"status": "ok", "total_requests": 0, "total_errors": 0, "blocked_rate": 0.0}`.
Un servicio totalmente caído indistinguible de uno ocioso. Nos enteramos porque
un tester mandó una captura de la pantalla de una causa.

Este módulo cierra las dos: cuenta el fallo en una métrica propia y manda una
alerta por el HECHO (no por la proporción), sin cambiar el 500 que ve la app.
"""

import logging

from fastapi import Request

from app.alerting import maybe_alert_event
from app.metrics import api_metrics

logger = logging.getLogger(__name__)

POOL_UNAVAILABLE_EVENT = "pool_unavailable"


async def acquire_or_alert(pool, request: Request, endpoint: str):
    """Devuelve una sesión del pool; si no puede, deja rastro y re-lanza.

    Re-lanza a propósito y no devuelve una respuesta `blocked=True`: eso haría
    que la app tratara una caída NUESTRA como un bloqueo de OJV, que es
    exactamente la atribución falsa que este trabajo vino a arreglar. El 5xx es
    lo que hace que la app la clasifique bien.
    """
    try:
        return await pool.acquire()
    except Exception as e:
        api_metrics.record_pool_failure(endpoint)
        logger.exception("Pool sin sesion disponible para %s", endpoint)
        await maybe_alert_event(
            request,
            POOL_UNAVAILABLE_EVENT,
            f"{endpoint}: el pool no pudo entregar sesion ({type(e).__name__}: {e}). "
            "Revisar bundles F5 / proxy residencial: el servicio no esta atendiendo.",
        )
        raise
