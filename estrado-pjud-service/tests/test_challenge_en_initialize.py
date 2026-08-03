"""El challenge de F5 se ve donde llega, no dos requests después.

La medición del VPS que sostiene todo esto —la tabla de bytes por IP, los tres
500 del journal, y que la excepción del transporte no identifica la causa— está
en el docstring de `BlockedPageError`. No se recopia acá, por lo mismo
que dice `test_detail_sin_csrf`: repetida en tres lados, se desactualiza en los
tres.

Lo propio de este archivo: lo estable es la PÁGINA. `detect_blocked` la reconoce
4 de 4 (el challenge por la IP del datacenter, las tres páginas buenas por los
slots residenciales), mientras que el mismo hecho salió como `RemoteProtocolError`
en producción y como `ReadError` en la medición.

Dónde vive cada afirmación: que `BlockedPageError` sea infra y que SÍ
re-mintee el slot se afirma en los parametrize canónicos de `test_failure_kind`,
que son el inventario único de las dos tablas.
"""

import httpx
import pytest

from app.failure_kind import (
    BlockedPageError,
    EmptyResponseError,
    MissingCsrfTokenError,
    new_egress_may_help,
)
from app.adapters.http_adapter import OJVHttpAdapter
from app.metrics import api_metrics
from app.session import OJVSession
from tests.helpers import (
    AdapterQueGraba,
    FakeOJVSession,
    cookie_bundle,
    http_status_error,
    infra_exceptions,
    pool_con_store,
)

_CHALLENGE = '<html><head><script>window["bobcmn"]="1011";</script></head><body></body></html>'
_PAGINA_REAL = (
    "<html><body><script>var cfg={token:'"
    + "a" * 32
    + "'};</script><select id=\"competencia\"></select></body></html>"
)


@pytest.mark.asyncio
async def test_challenge_corta_antes_del_post():
    """La página bloqueada aborta `initialize()` sin gastar el POST.

    El POST es el request que en producción se colgaba: sin este corte, cada
    arranque contra un bundle quemado paga un GET + un POST que muere, y el
    error que queda escrito habla del transporte y no del bloqueo.
    """
    adapter = AdapterQueGraba(html_get=_CHALLENGE)
    session = OJVSession(adapter)

    with pytest.raises(BlockedPageError):
        await session.initialize()

    assert adapter.gets == ["/consultaUnificada.php"]
    assert adapter.posts == [], "no debe gastar el POST contra una sesión ya bloqueada"


@pytest.mark.asyncio
async def test_cuerpo_vacio_no_se_reporta_como_challenge():
    """Cero bytes es el túnel cortándose, no un bloqueo — y se distinguen.

    `detect_blocked("")` devuelve True, así que sin `reject_empty_body` primero
    nuestra caída de red saldría escrita como challenge de F5. Los otros cuatro
    call sites ya ordenan los dos chequeos así; éste era el que faltaba.

    Hoy las dos excepciones caen en "infra", o sea que la app ve lo mismo. Lo
    que cambia es lo que queda escrito para el que diagnostique — que es todo el
    punto de esta PR.
    """
    adapter = AdapterQueGraba(html_get="")
    session = OJVSession(adapter)

    with pytest.raises(EmptyResponseError):
        await session.initialize()

    assert adapter.posts == []


@pytest.mark.asyncio
async def test_pagina_real_inicializa_normal():
    """Control: sin bloqueo, `initialize()` saca el token y activa la sesión."""
    adapter = AdapterQueGraba(html_get=_PAGINA_REAL)
    session = OJVSession(adapter)

    await session.initialize()

    assert session.csrf_token == "a" * 32
    assert [p for p, _ in adapter.posts] == ["/includes/sesion-invitado.php"]


@pytest.mark.parametrize("exc", infra_exceptions() + [BlockedPageError("challenge")])
def test_otro_egreso_ayuda_ante_infra(exc):
    """Si la culpa es nuestra y el re-mint ayuda, otro bundle es otra IP."""
    assert new_egress_may_help(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        MissingCsrfTokenError("regex sin match"),
        http_status_error(500),
        http_status_error(403),
        http_status_error(404),
    ],
    ids=lambda e: type(e).__name__,
)
def test_otro_egreso_no_ayuda(exc):
    """Reintentar por otra IP no arregla ni el drift de regex ni un no de OJV.

    Los 5xx/403 son del portal: con tres bundles quemaríamos las tres IPs para
    cobrar el mismo no tres veces. El 404 es de la causa. Y el drift de regex
    llega igual de mudo por cualquier IP — ver `slot_still_healthy`.
    """
    assert new_egress_may_help(exc) is False


# --- el pool de la API reintenta por otra IP -------------------------------


def _sesion_guionada(fallos):
    """Clase de sesión cuyo `initialize()` sigue el guión `fallos`.

    Una entrada por intento; `None` significa "este intento anda". Si el pool
    intenta más veces que las guionadas, `next()` levanta `StopIteration` — que
    es el ruido que se quiere ante un loop que no termina.
    """
    guion = iter(fallos)

    class _Sesion(FakeOJVSession):
        async def initialize(self):
            exc = next(guion)
            if exc is not None:
                raise exc

    return _Sesion


def _bundles(n):
    return {i: cookie_bundle(i, proxy_url=f"http://u:p@sticky{i}:1") for i in range(n)}


@pytest.mark.asyncio
async def test_reintenta_por_otro_bundle_y_sale_vivo(monkeypatch):
    """Un bundle quemado ya no es un 500: se reintenta por la IP siguiente.

    Ésta es la diferencia medida entre el worker y la API con los MISMOS
    bundles: el worker reintenta con IP nueva, la API probaba una sola y
    devolvía 500. Tres de los cuatro `/api/v1/search` autenticados de 10 días.
    """
    pool, capturados = pool_con_store(
        monkeypatch,
        _bundles(3),
        session_cls=_sesion_guionada([BlockedPageError("challenge"), None]),
    )

    await pool.acquire()

    assert [c["proxy"] for c in capturados] == [
        "http://u:p@sticky0:1",
        "http://u:p@sticky1:1",
    ], "el reintento tiene que salir por OTRA IP, no por la misma"


@pytest.mark.asyncio
async def test_el_reintento_queda_contado(monkeypatch):
    """Un pool degradado no puede verse igual que uno sano en `/health`.

    `record_pool_failure` sólo cuenta cuando se agotaron TODOS los bundles, así
    que sin este contador la respuesta es 200 y la única huella es un warning
    que nadie agrega: el reintento apagaría el instrumento que mide la falla que
    vino a tapar.
    """
    # Sin `reset()` manual: `conftest._reset_metrics` es autouse y ya corre
    # antes de cada test.
    pool, _ = pool_con_store(
        monkeypatch,
        _bundles(3),
        session_cls=_sesion_guionada([BlockedPageError("challenge"), None]),
    )

    await pool.acquire()

    assert api_metrics.snapshot()["total_bundle_retries"] == 1


@pytest.mark.asyncio
async def test_con_todos_los_bundles_quemados_levanta(monkeypatch):
    """Sin IP sana no se inventa una: se acaban los intentos y sale la excepción.

    El techo es la cantidad de bundles justamente para que esto no sea un loop:
    tres bundles, tres intentos, y la excepción del último llega entera a
    `acquire_or_alert` para que la clasifique.
    """
    pool, capturados = pool_con_store(
        monkeypatch,
        _bundles(3),
        session_cls=_sesion_guionada([BlockedPageError("challenge")] * 3),
    )

    with pytest.raises(BlockedPageError):
        await pool.acquire()

    assert len(capturados) == 3, "un intento por bundle, ni uno más"


@pytest.mark.asyncio
async def test_no_reintenta_cuando_otro_egreso_no_ayuda(monkeypatch):
    """Ante un 500 de OJV se sale al primer intento.

    Reintentar acá quemaría las tres IPs residenciales para cobrar el mismo no
    tres veces: el portal no contesta distinto porque cambiemos de casa.
    """
    pool, capturados = pool_con_store(
        monkeypatch,
        _bundles(3),
        session_cls=_sesion_guionada([http_status_error(500)]),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await pool.acquire()

    assert len(capturados) == 1


def test_el_presupuesto_es_menor_que_un_intento():
    """El presupuesto tiene que caber DENTRO del timeout de un intento.

    Si `_RETRY_BUDGET_S` supera el timeout del adapter, deja de acotar nada: un
    primer intento que se cuelga los 30 s todavía queda por debajo del límite,
    arranca un segundo, y vuelve el caso de los 3 minutos que este presupuesto
    vino a cerrar. El número exacto es una decisión; que sea menor que el
    timeout es el invariante.

    Se lee del cliente httpx de verdad y no de un literal, porque el que puede
    moverse sin que nadie mire es el timeout del adapter.
    """
    from app.session_pool import _RETRY_BUDGET_S
    from tests.helpers import api_settings

    adapter = OJVHttpAdapter(api_settings())
    assert _RETRY_BUDGET_S < adapter._client.timeout.read


@pytest.mark.asyncio
async def test_el_presupuesto_de_tiempo_corta_el_reintento(monkeypatch):
    """Un primer intento lento no arranca un segundo.

    El reintento vive en el camino de una request HTTP, no en un job de fondo:
    `ReadTimeout` pasa el filtro de `new_egress_may_help` —y hace bien, otra IP
    puede andar— pero son 30 s de `httpx.Timeout` por intento, así que sin
    presupuesto tres bundles son hasta 3 minutos donde antes se fallaba en 60 s.
    """
    from app import session_pool as sp

    # El presupuesto a cero, y NO un reloj falso: `sp.time` ES el módulo `time`,
    # así que parchearle `monotonic` se lo cambia a todo el proceso.
    monkeypatch.setattr(sp, "_RETRY_BUDGET_S", 0.0)

    pool, capturados = pool_con_store(
        monkeypatch,
        _bundles(3),
        session_cls=_sesion_guionada([httpx.ReadTimeout("lento"), None]),
    )

    with pytest.raises(httpx.ReadTimeout):
        await pool.acquire()

    assert len(capturados) == 1, "el presupuesto agotado no arranca otro intento"
