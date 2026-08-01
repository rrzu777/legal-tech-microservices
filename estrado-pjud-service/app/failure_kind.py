"""De quién es la culpa cuando una consulta a OJV falla.

Vive acá, en `app/`, y no en `worker/`, porque los dos lados lo necesitan: las
rutas HTTP para decidir qué contestarle a la app, y el worker para decidir si le
suma una falla a la causa. Que la regla estuviera escrita cuatro veces —dos
`except` y dos `isinstance`, las cuatro distintas— es precisamente cómo los dos
servicios dejaron de ser espejos: la app clasificaba un 503 como transitorio y
el worker lo dejaba caer en su `except Exception`, o sea contador++ y, a las 10,
`suspended`.

⚠️ Esto NO es el espejo de `pjudHttpError` (`apps/web/src/lib/pjud/client.ts`),
aunque comparta la tabla `{>=500, 401, 403, 429}`. Las etiquetas son OPUESTAS a
propósito, porque el upstream es otro: allá esos status los devuelve NUESTRO
microservicio, y por eso allá son "infra"; acá los devuelve OJV, y por eso acá
son "ojv". Alinearlas rompería la atribución en las dos puntas. Lo que sí tiene
que espejarse entre los repos es el vocabulario del desenlace —`BlockCause`,
`FamiliaErrorCode`—, no esta tabla.

Las tres respuestas:

- **infra** — nuestro lado. La IP residencial se cayó, el proxy no levantó el
  túnel, se acabó el tiempo, el cuerpo llegó vacío. No penaliza la causa y el
  slot se re-mintea.
- **ojv** — el portal contestó que no. Un 403, un 429, un 5xx de ellos. Tampoco
  penaliza: la causa no tiene la culpa de que su servidor esté caído.
- **case** — todo lo demás: el parser no entendió, el identificador no existe,
  el 404. Esto SÍ es de la causa y sí cuenta.

Sobre los 5xx: pasan por "ojv" y no por "infra" porque OJV es HTTPS y nuestro
proxy residencial habla por CONNECT — una vez que el túnel está armado, el proxy
ya no puede inventar una respuesta HTTP, así que el status viene del origen. Los
fallos del proxy en sí llegan como `httpx.ProxyError`, que es `TransportError`,
o sea "infra". La distinción se sostiene en esa propiedad del transporte, no en
una corazonada.
"""

from __future__ import annotations

from typing import Literal

import httpx

FailureKind = Literal["infra", "ojv", "case"]

#: De quién fue la culpa de un bloqueo, para el texto que ve el abogado.
BlockCause = Literal["ojv", "infra"]

#: Los 4xx que NO son de la causa. 401/403 es el portal rechazando la sesión,
#: 429 es rate limiting. El resto de los 4xx —un 404 sobre el detalle— sí
#: describe algo puntual del pedido.
_OJV_REJECTION_STATUSES = frozenset({401, 403, 429})


class EmptyResponseError(Exception):
    """OJV contestó 200 con un cuerpo vacío o de dos líneas.

    Es infra y no un bloqueo, aunque durante mucho tiempo se contó como bloqueo:
    el F5 devuelve una página de challenge REAL (`detect_blocked` la reconoce),
    no un cuerpo vacío. Un cuerpo vacío es el túnel cortándose a mitad de la
    respuesta. Colapsar las dos señales cuando son distinguibles hacía que
    nuestra caída de red se leyera en el panel como "OJV nos bloqueó" — la
    confusión que sostuvo el outage de dos meses y medio.
    """


def classify_exception(e: BaseException) -> FailureKind:
    """De quién es la culpa de esta excepción."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status >= 500 or status in _OJV_REJECTION_STATUSES:
            return "ojv"
        return "case"

    # `httpx.TimeoutException` ya es `TransportError`; `TimeoutError` cubre el
    # `asyncio.timeout` (que en 3.11+ es el mismo tipo) y `EmptyResponseError`
    # entra por su cuenta.
    if isinstance(e, (httpx.TransportError, TimeoutError, EmptyResponseError)):
        return "infra"

    return "case"


def block_cause(e: BaseException) -> BlockCause:
    """La causa a escribir cuando YA se decidió que esto es un bloqueo.

    Colapsa "case" en "infra" a propósito: quien llama ya resolvió que el
    desenlace es un bloqueo transitorio, así que lo único que falta es de quién
    fue. Si el portal no contestó que no, la culpa es nuestra — y ante la duda
    preferimos cargárnosla nosotros antes que acusar al Poder Judicial.
    """
    return "ojv" if classify_exception(e) == "ojv" else "infra"


def reject_empty_body(html: str | None, step: str) -> None:
    """Un cuerpo de CERO bytes no es un bloqueo: es el túnel cortándose.

    La distinción es de un byte y vale la pena escribirla. Una página
    contentless de ~39 bytes (`<html><head></head><body></body></html>`) SÍ es un
    soft-block de F5: está medida, tiene su test
    (`test_returns_blocked_on_contentless_soft_block`) y por eso el `len < 100`
    de los call sites se queda donde está. Pero cero bytes no es una página que
    alguien haya servido a propósito — es la respuesta que no llegó.
    Cargársela a OJV es acusarlos de nuestra caída de red, y además le escribe al
    abogado "bloqueado por OJV" cuando lo que hay que revisar es el proxy.

    Vive acá y no en `worker/engine.py` porque los cuatro call sites —las dos
    rutas y los dos helpers del worker— ya importan de este módulo, y tenerla en
    `worker/` obligaba a las rutas de `app/` a reescribir el predicado a mano.
    Ahí estaba pasando: dos ortografías del mismo chequeo y el mensaje tipeado
    tres veces.
    """
    if html is None or not html.strip():
        raise EmptyResponseError(f"{step}: OJV devolvio un cuerpo vacio")
