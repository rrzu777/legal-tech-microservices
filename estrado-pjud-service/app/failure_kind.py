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

from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError

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


class UpstreamChangedError(Exception):
    """PJUD returned a non-empty response that no longer matches our parser.

    This is neither a missing case nor a request-validation error.  Routes must
    surface it as a retryable server-side failure, avoiding a false 409 during
    detail session affinity or a silent false not-found.
    """


class NoUsableBundleError(Exception):
    """El pool no tiene ningún bundle F5 con el cual salir.

    O el store está vacío (el worker todavía no minteó) o los bundles que hay no
    traen `proxy_url`. Las dos son infra NUESTRA, y en las dos la única respuesta
    correcta es no salir.

    Antes de esto el pool caía a sin-proxy/sin-cookies y egresaba por la IP del
    datacenter. Medido el 1 de agosto de 2026: por esa IP, OJV contesta HTTP 200
    con una página de challenge de F5 de ~4900 bytes que `detect_blocked`
    reconoce — o sea que nuestra decisión de salir sin proxy terminaba reportada
    como "OJV nos bloqueó". Encima gasta reputación de la IP en cada intento.
    """


class BlockedPageError(Exception):
    """Una página que debía traer contenido vino con el challenge de F5.

    El caso fundante es `initialize()`: abre `/consultaUnificada.php` para
    juntar cookies y token, y si esa página está bloqueada la sesión ya nació
    inservible — pero el bloqueo no se veía: sale con **HTTP 200**, así que
    `raise_for_status()` lo deja pasar. `fetch_anexo_list()` tiene la misma
    forma con otro desenlace: el challenge no matchea ninguna tabla, el parser
    devuelve lista vacía y los anexos de ese folio se pierden EN SILENCIO como
    "No anexo files found".

    Medido en el VPS el 3 de agosto de 2026 (una corrida, `www-data`, config
    real): por la IP del datacenter la GET da 200 con 4.901 bytes, sin token,
    `detect_blocked` True y `bobcmn` presente, y el POST siguiente a
    `sesion-invitado.php` sobre esa misma sesión **muere en el transporte**. Por
    los tres slots residenciales, la misma GET da 200 con 186.008 bytes, con
    token, sin bloqueo, y el POST contesta 200.

    Eso producía los 500 de `/api/v1/search`: tres en el journal (31 jul 23:49,
    1 ago 00:04, 1 ago 03:32), los tres precedidos ~400 ms antes por
    `CSRF token not found in initial page`, y el único arranque que sí logueó
    `CSRF token acquired` es el único que no falló.

    ⚠️ La excepción del transporte NO identifica la causa: producción escribió
    `RemoteProtocolError` y la medición `ReadError`. Son la misma cosa vista
    desde abajo —la conexión cortada— y ninguna dice que hubo un challenge.
    Diagnosticar desde ahí costó diez días de journal; por eso el corte va donde
    la señal es estable, que es la página.

    Es infra: lo que F5 rechazó es la IP con la que salimos, no la causa ni el
    portal. `detect_blocked` ya se aplicaba en search y en detail; ésta era la
    única respuesta del ciclo de vida de una sesión que no pasaba por él.
    """


class MissingCsrfTokenError(Exception):
    """`detail()` no tiene token CSRF con el cual salir.

    `initialize()` lo saca de `consultaUnificada.php` con un regex; si el markup
    de PJUD cambia, el regex deja de matchear, la sesión se queda con
    `csrf_token = None` y hasta acá eso era un WARNING en el log y nada más.

    Medido contra OJV el 2 de agosto de 2026 (misma sesión, mismo JWT, variando
    sólo el campo `token`): con el token real, HTTP 200 y 45 KB de expediente;
    con `None`, con `""` y sin la clave, **HTTP 405 y cero bytes** las tres
    veces. El token no es decorativo.

    Y 405 no está en `_OJV_REJECTION_STATUSES`, así que el `HTTPStatusError` que
    salía de ahí caía en "case": el único veredicto que le suma al contador de la
    causa. Diez de esos y queda `suspended`, que es terminal — por un defecto
    nuestro, detectable una etapa antes.

    Lo levanta `detail()` y no `initialize()` a propósito: `search()` no manda el
    token y funciona perfecto sin él, así que abortar el arranque apagaría
    también la búsqueda, que no tiene el problema.

    Desde `BlockedPageError`, esto significa SÓLO drift de regex: la otra
    forma de quedarse sin token —que la página fuera un challenge— se corta antes,
    en `initialize()`. El veredicto opuesto que eso les da está en
    `slot_still_healthy`, que es donde se decide.
    """


class RejectedDetailSessionError(Exception):
    """OJV rechazó con 405 vacío una sesión que sí llevaba token CSRF.

    Este patrón no describe a la causa. En producción apareció después de que
    el mismo slot completara correctamente una consulta anterior: search seguía
    respondiendo, pero detail rechazaba la sesión reutilizada. Una sesión nueva
    sí puede corregirlo, por lo que el worker debe descartar y re-mintear el slot.
    """


def slot_still_healthy(e: BaseException) -> bool:
    """¿El slot F5 sigue sirviendo pese a esta excepción?

    Sólo tiene sentido preguntarlo en el camino infra/ojv de `sync_case`: ahí el
    worker venía marcando `session_healthy = False` para todo, y eso dispara
    re-mint del slot —cooldown de 30 s, Chromium y una IP sticky nueva—. Para un
    proxy caído o un bloqueo de F5 eso es exactamente lo que hay que hacer.

    Para `MissingCsrfTokenError` no: la página llegó entera por esa IP, y el
    re-mint termina en `initialize()`, que corre el MISMO regex contra el MISMO
    markup y devuelve otra sesión igual de muda. Sería un loop indefinido
    quemando una IP residencial y 30 s por causa sin poder corregir nunca la
    condición — el arreglo de atribución cambiando una factura por otra.

    `BlockedPageError` NO está acá, y es la contracara exacta: el re-mint
    cambia la IP sticky, y la IP es justo lo que F5 rechazó. Página entera sin
    token = drift, no re-mintear; página de challenge = IP quemada, re-mintear.

    Es el mismo razonamiento que ya está escrito para `parse_suspect` en
    `worker/engine.py`, que deja el slot sano a propósito porque ante drift de
    página o de parser re-mintear no ayuda.
    """
    return isinstance(e, MissingCsrfTokenError)


def new_egress_may_help(e: BaseException) -> bool:
    """¿Reintentar por otra IP residencial puede cambiar el resultado?

    Se deriva de las dos preguntas que ya están respondidas más arriba en vez de
    abrir una tercera lista: tiene que ser culpa NUESTRA (`infra` — si el portal
    contestó que no, con tres bundles quemaríamos tres IPs para cobrar el mismo
    no tres veces) y tiene que ser de las que el re-mint corrige (`not
    slot_still_healthy` — porque "otro bundle" y "re-mint" compran exactamente lo
    mismo: otra IP de salida).

    Lo consume `APISessionPool.acquire()`, que hasta acá probaba UN bundle y
    devolvía 500. El worker, con los mismos bundles, reintenta hasta
    `MINT_MAX_RETRIES` veces con IP nueva — y ésa es toda la diferencia entre sus
    ~50 sesiones sanas en 10 días y los 3 de 4 `/api/v1/search` que se cayeron.

    ⚠️ Hoy la regla la obedece UN solo loop. `worker/session_pool.py::_mint_slot`
    reintenta sobre un `except Exception` pelado, así que un 5xx de OJV durante
    el minteo le quema `MINT_MAX_RETRIES` IPs para cobrar el mismo no — que es
    justo lo que esto dice que no hay que hacer. Es preexistente y cambiarlo
    merece su propia medición; queda anotado para que nadie lea este predicado
    como si ya rigiera en los dos lados.
    """
    return classify_exception(e) == "infra" and not slot_still_healthy(e)


def classify_exception(e: BaseException) -> FailureKind:
    """De quién es la culpa de esta excepción."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status >= 500 or status in _OJV_REJECTION_STATUSES:
            return "ojv"
        return "case"

    # `httpx.TimeoutException` ya es `TransportError`; `TimeoutError` cubre el
    # `asyncio.timeout` (que en 3.11+ es el mismo tipo), y `EmptyResponseError`,
    # `NoUsableBundleError`, `MissingCsrfTokenError` y `BlockedPageError`
    # entran por su cuenta.
    if isinstance(
        e,
        (
            httpx.TransportError,
            TimeoutError,
            EmptyResponseError,
            UpstreamChangedError,
            NoUsableBundleError,
            MissingCsrfTokenError,
            RejectedDetailSessionError,
            BlockedPageError,
            ProxyBudgetExceededError,
            ProxyUsagePersistenceError,
        ),
    ):
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
