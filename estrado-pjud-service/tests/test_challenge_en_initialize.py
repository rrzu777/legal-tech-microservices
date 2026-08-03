"""El challenge de F5 se ve donde llega, no dos requests después.

Medido en el VPS el 3 de agosto de 2026, con `www-data` y la config real, una
GET de `/consultaUnificada.php` seguida del POST a `sesion-invitado.php` sobre la
MISMA sesión:

| egreso                 | GET | bytes   | token | detect_blocked | POST siguiente |
|------------------------|-----|---------|-------|----------------|----------------|
| sin proxy (datacenter) | 200 |   4.901 | no    | True           | 💥 ReadError   |
| slot 0 (residencial)   | 200 | 186.008 | sí    | False          | 200, 149 bytes |
| slot 1 (residencial)   | 200 | 186.008 | sí    | False          | 200, 149 bytes |
| slot 2 (residencial)   | 200 | 186.008 | sí    | False          | 200, 149 bytes |

O sea: el challenge sale con **HTTP 200**, así que `raise_for_status()` lo deja
pasar; el regex del token no matchea y eso era un WARNING y nada más; y el POST
que venía justo después moría en el transporte.

Eso explica los tres 500 de `/api/v1/search` del journal (31 jul 23:49, 1 ago
00:04, 1 ago 03:32): los tres precedidos, ~400 ms antes, por
`CSRF token not found in initial page`, y el único arranque que sí logueó
`CSRF token acquired` es el único que no falló.

⚠️ La excepción del transporte NO es estable: en producción salió
`RemoteProtocolError`, en la medición salió `ReadError`. Las dos son
`httpx.TransportError` y las dos dicen "la conexión se cortó", pero ninguna dice
POR QUÉ — por eso el diagnóstico no puede colgar de la clase de la excepción. Lo
estable es la página, y `detect_blocked` la reconoce 4/4.
"""

from unittest.mock import MagicMock

import httpx
import pytest

from app.failure_kind import (
    BlockedInitialPageError,
    MissingCsrfTokenError,
    classify_exception,
    new_egress_may_help,
    slot_still_healthy,
)
from app.session import OJVSession
from tests.helpers import FakeOJVSession, http_status_error, infra_exceptions

_CHALLENGE = '<html><head><script>window["bobcmn"]="1011";</script></head><body></body></html>'
_PAGINA_REAL = (
    "<html><body><script>var cfg={token:'"
    + "a" * 32
    + "'};</script><select id=\"competencia\"></select></body></html>"
)


class _AdapterQueGraba:
    """Adapter que devuelve `httpx.Response` de verdad y anota cada request.

    Real y no un stub: `initialize()` llama `raise_for_status()` y decodifica el
    cuerpo, así que un doble que devuelva un objeto cualquiera no ejercita nada
    de eso. `posts` es lo que decide el test principal: la pregunta no es sólo
    "¿levantó?", es "¿levantó ANTES de gastar el POST?".
    """

    def __init__(self, html: str):
        self._html = html
        self.gets: list[str] = []
        self.posts: list[str] = []

    async def get(self, path, **kwargs):
        self.gets.append(path)
        return httpx.Response(
            200,
            content=self._html.encode("utf-8"),
            request=httpx.Request("GET", f"https://ojv.test{path}"),
        )

    async def post(self, path, **kwargs):
        self.posts.append(path)
        return httpx.Response(
            200,
            content=b"ok",
            request=httpx.Request("POST", f"https://ojv.test{path}"),
        )


@pytest.mark.asyncio
async def test_challenge_corta_antes_del_post():
    """La página bloqueada aborta `initialize()` sin gastar el POST.

    El POST es el request que en producción se colgaba: sin este corte, cada
    arranque contra un bundle quemado paga un GET + un POST que muere, y el
    error que queda escrito habla del transporte y no del bloqueo.
    """
    adapter = _AdapterQueGraba(_CHALLENGE)
    session = OJVSession(adapter)

    with pytest.raises(BlockedInitialPageError):
        await session.initialize()

    assert adapter.gets == ["/consultaUnificada.php"]
    assert adapter.posts == [], "no debe gastar el POST contra una sesión ya bloqueada"


@pytest.mark.asyncio
async def test_pagina_real_inicializa_normal():
    """Control: sin bloqueo, `initialize()` saca el token y activa la sesión."""
    adapter = _AdapterQueGraba(_PAGINA_REAL)
    session = OJVSession(adapter)

    await session.initialize()

    assert session.csrf_token == "a" * 32
    assert adapter.posts == ["/includes/sesion-invitado.php"]


def test_challenge_es_infra():
    """Es nuestro: la IP/bundle con el que salimos es lo que F5 rechazó.

    No es "ojv" —el portal no contestó que no, contestó un acertijo— y sobre todo
    no es "case", que es el único veredicto que le suma al contador y a las 10
    la deja `suspended`.
    """
    assert classify_exception(BlockedInitialPageError("challenge")) == "infra"


def test_el_challenge_si_re_mintea_el_slot():
    """Acá el re-mint SÍ corresponde, al revés que en `MissingCsrfTokenError`.

    Re-mintear cambia la IP sticky, y la IP es exactamente lo que F5 está
    rechazando. Es la distinción que `slot_still_healthy` existe para hacer: el
    token ausente sobre una página entera es drift de regex y el re-mint no
    puede corregirlo; una página de challenge es la IP quemada y el re-mint es
    la corrección.
    """
    assert slot_still_healthy(BlockedInitialPageError("challenge")) is False
    assert slot_still_healthy(MissingCsrfTokenError("regex sin match")) is True


@pytest.mark.parametrize("exc", infra_exceptions() + [BlockedInitialPageError("challenge")])
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
)
def test_otro_egreso_no_ayuda(exc):
    """Reintentar por otra IP no arregla ni el drift de regex ni un no de OJV.

    Los 5xx/403 son del portal: con tres bundles quemaríamos las tres IPs para
    cobrar el mismo no tres veces. Y el 404 es de la causa.
    """
    assert new_egress_may_help(exc) is False


# --- el pool de la API reintenta por otra IP -------------------------------


def _pool_con_bundles(monkeypatch, n, fallos):
    """Pool con `n` bundles y una `initialize()` guionada por `fallos`.

    `fallos` es la lista de excepciones a levantar, una por intento; un `None`
    significa "este intento anda". Devuelve (pool, salidas), donde `salidas`
    anota el `proxy` de cada intento — que es como se ve si de verdad rotó de IP
    y no reintentó contra la misma.
    """
    import time

    from app import session_pool as sp
    from app.cookie_store import CookieBundle
    from app.session_pool import APISessionPool
    from tests.helpers import api_settings

    pool = APISessionPool(api_settings())
    pool._store = MagicMock()
    pool._store.load_all.return_value = {
        i: CookieBundle(
            cookies={"TSPD_101": f"c{i}"},
            user_agent=f"UA{i}",
            saved_at=time.time(),
            proxy_url=f"http://u:p@sticky{i}:1",
        )
        for i in range(n)
    }

    salidas = []
    guion = iter(fallos)

    class _Session(FakeOJVSession):
        async def initialize(self):
            salidas.append(self.adapter["proxy"])
            exc = next(guion)
            if exc is not None:
                raise exc

    monkeypatch.setattr(
        sp, "OJVHttpAdapter",
        lambda s, proxy=None, user_agent=None, cookies=None: {"proxy": proxy},
    )
    monkeypatch.setattr(sp, "OJVSession", _Session)
    return pool, salidas


@pytest.mark.asyncio
async def test_reintenta_por_otro_bundle_y_sale_vivo(monkeypatch):
    """Un bundle quemado ya no es un 500: se reintenta por la IP siguiente.

    Ésta es la diferencia medida entre el worker y la API con los MISMOS
    bundles: el worker reintenta con IP nueva, la API probaba una sola y
    devolvia 500. Tres de los cuatro `/api/v1/search` autenticados de 10 dias.
    """
    pool, salidas = _pool_con_bundles(
        monkeypatch, 3, [BlockedInitialPageError("challenge"), None]
    )

    session = await pool.acquire()

    assert session is not None
    assert salidas == ["http://u:p@sticky0:1", "http://u:p@sticky1:1"], (
        "el reintento tiene que salir por OTRA IP, no por la misma"
    )


@pytest.mark.asyncio
async def test_con_todos_los_bundles_quemados_levanta(monkeypatch):
    """Sin IP sana no se inventa una: se acaban los intentos y sale la excepción.

    El techo es la cantidad de bundles justamente para que esto no sea un loop:
    tres bundles, tres intentos, y la excepción del último llega entera a
    `acquire_or_alert` para que la clasifique.
    """
    pool, salidas = _pool_con_bundles(
        monkeypatch, 3, [BlockedInitialPageError(f"challenge {i}") for i in range(3)]
    )

    with pytest.raises(BlockedInitialPageError):
        await pool.acquire()

    assert len(salidas) == 3, "un intento por bundle, ni uno más"


@pytest.mark.asyncio
async def test_no_reintenta_cuando_otro_egreso_no_ayuda(monkeypatch):
    """Ante un 500 de OJV se sale al primer intento.

    Reintentar acá quemaría las tres IPs residenciales para cobrar el mismo no
    tres veces: el portal no contesta distinto porque cambiemos de casa.
    """
    pool, salidas = _pool_con_bundles(
        monkeypatch, 3, [http_status_error(500), None, None]
    )

    with pytest.raises(httpx.HTTPStatusError):
        await pool.acquire()

    assert len(salidas) == 1
