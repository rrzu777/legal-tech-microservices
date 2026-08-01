"""El clasificador que hace que los dos servicios sean espejos.

El valor de este módulo no está en ninguna de las tres respuestas por separado,
está en que haya UNA sola tabla. Antes la regla estaba escrita cuatro veces —dos
`except` en el worker y dos `isinstance` en las rutas—, las cuatro distintas, y
por eso el mismo 503 de OJV suspendía la causa o no según quién la tomara.
"""

import httpx
import pytest

from app.failure_kind import EmptyResponseError, block_cause, classify_exception
from tests.helpers import http_status_error as _status, infra_exceptions


@pytest.mark.parametrize(
    "exc",
    [*infra_exceptions(), TimeoutError("asyncio"), EmptyResponseError("cuerpo vacio")],
    ids=lambda e: type(e).__name__,
)
def test_nuestras_caidas_son_infra(exc):
    assert classify_exception(exc) == "infra"


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


def test_block_cause_ante_la_duda_se_la_carga_a_nuestro_lado():
    """`block_cause` se llama cuando YA se decidió que esto es un bloqueo
    transitorio; lo único que falta es de quién. Si el portal no contestó que no,
    la culpa es nuestra: preferimos cargárnosla antes que acusar al Poder
    Judicial de algo que no hizo."""
    assert block_cause(_status(503)) == "ojv"
    assert block_cause(httpx.ProxyError("proxy down")) == "infra"
    assert block_cause(ValueError("cualquier cosa")) == "infra"
