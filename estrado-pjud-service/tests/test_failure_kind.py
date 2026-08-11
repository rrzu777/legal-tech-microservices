"""El clasificador que hace que los dos servicios sean espejos.

El valor de este módulo no está en ninguna de las tres respuestas por separado,
está en que haya UNA sola tabla. Antes la regla estaba escrita cuatro veces —dos
`except` en el worker y dos `isinstance` en las rutas—, las cuatro distintas, y
por eso el mismo 503 de OJV suspendía la causa o no según quién la tomara.
"""

import httpx
import pytest

from app.failure_kind import (
    BlockedPageError,
    EmptyResponseError,
    MintUnavailableError,
    MissingCsrfTokenError,
    NoUsableBundleError,
    block_cause,
    classify_exception,
    new_egress_may_help,
    slot_still_healthy,
)
from tests.helpers import http_status_error as _status, infra_exceptions


@pytest.mark.parametrize(
    "exc",
    [
        *infra_exceptions(),
        TimeoutError("asyncio"),
        EmptyResponseError("cuerpo vacio"),
        # Quedarse sin bundle F5 vigente es infra NUESTRA. Sin esta línea el
        # arreglo del pool se muerde la cola: lo desconocido cae en "case", así
        # que la excepción saldría como culpa de la causa y la ruta contestaría
        # 200 `found=False` — el mismo defecto, por la puerta de atrás.
        NoUsableBundleError("sin bundle vigente"),
        # Sin token CSRF, OJV contesta 405 — y 405 cae en "case", o sea que la
        # causa se comía el contador por un regex nuestro que dejó de matchear.
        MissingCsrfTokenError("regex sin match"),
        # El challenge de F5 en la página inicial: lo que rechazan es la IP con
        # la que salimos, no la causa. Sale con HTTP 200, así que sin excepción
        # propia se colaba y reaparecía como error de transporte dos requests
        # después.
        BlockedPageError("challenge en consultaUnificada"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_nuestras_caidas_son_infra(exc):
    assert classify_exception(exc) == "infra"


@pytest.mark.parametrize(
    "exc",
    [
        *infra_exceptions(),
        EmptyResponseError("cuerpo vacio"),
        NoUsableBundleError("sin bundle"),
        # La contracara exacta de `MissingCsrfTokenError`, que sí deja el slot
        # sano: acá el re-mint cambia la IP sticky, y la IP es justo lo que F5
        # rechazó.
        BlockedPageError("challenge en consultaUnificada"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_lo_demas_si_re_mintea_el_slot(exc):
    """La contracara de `slot_still_healthy`.

    Un proxy caído o un bundle vencido SÍ se arreglan con una sesión nueva. Sin
    esta lista, alguien podría hacer que la guardia del CSRF apagara el re-mint
    para todo y ningún test se quejaría — y ahí el pool dejaría de recuperarse
    solo de la falla para la que fue construido.
    """
    assert slot_still_healthy(exc) is False


@pytest.mark.parametrize("code", [500, 502, 503, 504, 401, 403, 429])
def test_el_portal_contestando_que_no_es_de_ojv(code):
    """5xx y los tres 4xx que no describen el pedido.

    Los 5xx pasan por "ojv" y no por "infra" porque OJV es HTTPS y el proxy
    residencial habla por CONNECT: una vez armado el túnel, el proxy ya no puede
    inventar una respuesta HTTP, así que el status vino del origen. Sus fallos
    propios llegan como `ProxyError`, que es `TransportError`.
    """
    assert classify_exception(_status(code)) == "ojv"


@pytest.mark.parametrize("code", [400, 404, 410, 422])
def test_los_4xx_que_si_describen_el_pedido_son_de_la_causa(code):
    assert classify_exception(_status(code)) == "case"


def test_un_bug_nuestro_sigue_siendo_de_la_causa():
    """El default. Sin esto, "no penalizar nunca" pasaría todos los otros tests
    y el techo de suspensión quedaría muerto en silencio."""
    assert classify_exception(ValueError("bug de parseo")) == "case"


@pytest.mark.parametrize("code", [
    "browser_unavailable",
    "navigation_failed",
    "form_timeout",
    "deadline_exceeded",
])
def test_mint_unavailable_is_retryable_infra(code):
    """Removing the typed mint classification must stop safe IP rotation."""
    exc = MintUnavailableError(code)

    assert classify_exception(exc) == "infra"
    assert new_egress_may_help(exc) is True


def test_block_cause_ante_la_duda_se_la_carga_a_nuestro_lado():
    """`block_cause` se llama cuando YA se decidió que esto es un bloqueo
    transitorio; lo único que falta es de quién. Si el portal no contestó que no,
    la culpa es nuestra: preferimos cargárnosla antes que acusar al Poder
    Judicial de algo que no hizo."""
    assert block_cause(_status(503)) == "ojv"
    assert block_cause(httpx.ProxyError("proxy down")) == "infra"
    assert block_cause(ValueError("cualquier cosa")) == "infra"
