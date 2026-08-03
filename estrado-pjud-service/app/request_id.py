"""Correlation ID de punta a punta con la app.

Cada incidente hasta ahora (el RemoteProtocolError de agosto, la atribución
infra/OJV) se reconstruyó cruzando a mano los logs de Vercel con el journal del
VPS, emparejando por timestamp. Con esto, la app manda `X-Request-ID` en cada
request al microservicio, acá viaja en un contextvar, sale en CADA línea de log
que se emita dentro del request (`[rid=...]`) y vuelve en la respuesta — buscar
el rid en los dos lados ES la correlación.

El valor se valida antes de usarse: el header lo pone un cliente y va derecho
al log — sin la guarda, un `X-Request-ID: x\n[ERROR] fake` inyectaría líneas
falsas en el journal. Un valor inválido o ausente no es error: se acuña uno
nuevo (el request igual necesita identidad para sus propios logs).
"""

import contextvars
import logging
import re
import uuid

from fastapi import Request

# "-" y no None: es lo que imprime el log fuera de un request (arranque,
# worker), y un guion se lee como "no aplica" sin romper el formato.
request_id_var = contextvars.ContextVar("request_id", default="-")

_VALID_RID = re.compile(r"[A-Za-z0-9._-]{8,64}")

REQUEST_ID_HEADER = "X-Request-ID"


def normalize_request_id(raw: str | None) -> str:
    if raw and _VALID_RID.fullmatch(raw):
        return raw
    return uuid.uuid4().hex


class RequestIdFilter(logging.Filter):
    """Copia el contextvar a cada LogRecord como `rid`.

    Va como filter y no en cada llamada a log: los logger de app.* ya existen
    y ninguno sabe de request IDs; así siguen sin saber.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.rid = request_id_var.get()
        return True


async def request_id_middleware(request: Request, call_next):
    rid = normalize_request_id(request.headers.get(REQUEST_ID_HEADER))
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        # Hoy inobservable: BaseHTTPMiddleware corre cada capa en su propia
        # task con contexto COPIADO, así que el set nunca sale de acá (se
        # verificó por mutación: sin el reset no hay test que falle). Queda
        # igual porque es la semántica correcta de contextvar, y se vuelve
        # load-bearing si esto migra a middleware ASGI puro (sin task por capa).
        request_id_var.reset(token)
    response.headers[REQUEST_ID_HEADER] = rid
    return response
