"""Correlation ID de punta a punta con la app.

Cada incidente hasta ahora (el RemoteProtocolError de agosto, la atribución
infra/OJV) se reconstruyó cruzando a mano los logs de Vercel con el journal del
VPS, emparejando por timestamp. Con esto, la app manda `X-Request-ID` en cada
request al microservicio, acá viaja en un contextvar, sale en cada línea de log
de `app.*` emitida dentro del request (`[rid=...]`) y vuelve en la respuesta —
buscar el rid en los dos lados ES la correlación. (Los access logs de uvicorn
tienen handler propio con propagate=False y NO llevan rid; la reconstrucción
usa los logs de app.*, que sí.)

El valor se valida antes de usarse: el header lo pone un cliente y va derecho
al log — sin la guarda, un `X-Request-ID: x\n[ERROR] fake` inyectaría líneas
falsas en el journal. Un valor inválido o ausente no es error: se acuña uno
nuevo (el request igual necesita identidad para sus propios logs).
"""

import contextvars
import logging
import re
import uuid

# "-" y no None: es lo que imprime el log fuera de un request (arranque,
# worker), y un guion se lee como "no aplica" sin romper el formato.
request_id_var = contextvars.ContextVar("request_id", default="-")

_VALID_RID = re.compile(r"[A-Za-z0-9._-]{8,64}")

REQUEST_ID_HEADER = "X-Request-ID"

# El formato vive acá y no en main.py: es el contrato del filter (%(rid)s solo
# renderiza si RequestIdFilter corrió), y el test lo importa en vez de copiar
# el string — copiado, el test seguiría verde con el formato real ya driftado.
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s [rid=%(rid)s]: %(message)s"


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


class RequestIdMiddleware:
    """ASGI puro a propósito, no BaseHTTPMiddleware ni @app.middleware("http"):
    aquellos corren cada capa en una task nueva por request (costo extra y
    contexto COPIADO, con lo que el reset del contextvar sería inobservable).
    Acá todo pasa en la misma task que el resto del pipeline: sin overhead y
    con la semántica de contextvar de verdad.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = None
        for key, value in scope["headers"]:
            if key == b"x-request-id":
                raw = value.decode("latin-1")
                break
        rid = normalize_request_id(raw)
        rid_bytes = rid.encode("ascii")

        async def send_con_rid(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", rid_bytes),
                ]
            await send(message)

        token = request_id_var.set(rid)
        try:
            await self.app(scope, receive, send_con_rid)
        finally:
            request_id_var.reset(token)
