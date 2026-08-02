"""Sin token CSRF, `detail()` no sale — y la falla es NUESTRA, no de la causa.

Medido contra OJV el 2 de agosto de 2026, misma sesión y mismo JWT, cambiando
sólo el campo `token` del POST a `causaCivil.php`:

    token real          -> HTTP 200, 45.071 bytes, con movimientos
    token=None          -> HTTP 405, 0 bytes
    token=""            -> HTTP 405, 0 bytes
    sin la clave token  -> HTTP 405, 0 bytes

O sea: el token no es decorativo. Y 405 no está en `_OJV_REJECTION_STATUSES`, así
que `classify_exception` lo mandaba a **"case"** — el único de los tres veredictos
que le suma al contador de la causa. Diez de esos y la causa queda `suspended`,
que es terminal, por un defecto que es cien por ciento nuestro y que se detecta
una etapa antes, en `initialize()`.

El regex de `initialize()` matchea hoy (medido el mismo día: 5 hits en la página
de 186 KB), así que esto no es un incendio: es la trampa que queda armada para el
día que PJUD cambie el markup. Justamente por eso el arreglo va acá y no en
`initialize()`: `search()` no manda el token y anda perfecto sin él, así que
hacer fallar el arranque apagaría también la búsqueda, que no tiene el problema.
"""

import pytest

from app.failure_kind import MissingCsrfTokenError, classify_exception
from app.session import OJVSession


class _AdapterQueSeQueja:
    """Adapter que falla si alguien lo usa. La aserción es que NO se sale."""

    def __init__(self):
        self.posts = []

    async def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        raise AssertionError("no se debería haber salido a OJV sin token CSRF")

    async def close(self):
        pass


@pytest.mark.parametrize("token", [None, ""], ids=["None", "cadena-vacia"])
async def test_detail_sin_token_no_sale_a_la_calle(token):
    adapter = _AdapterQueSeQueja()
    session = OJVSession(adapter)
    session.csrf_token = token

    with pytest.raises(MissingCsrfTokenError):
        await session.detail("civil", "jwt.de.mentira")

    # Lo importante no es sólo que levante: es que no gastó un request. Salir
    # igual quema reputación de la IP residencial para cobrar un 405 seguro.
    assert adapter.posts == []


async def test_detail_con_token_manda_el_token_en_el_cuerpo():
    """Control: con token sí sale, y el token viaja donde OJV lo espera."""

    class _AdapterQueGraba:
        def __init__(self):
            self.data = None

        async def post(self, path, **kwargs):
            self.data = kwargs.get("data")
            return _RespuestaFalsa()

    class _RespuestaFalsa:
        status_code = 200
        content = b"<html>ok</html>"

        def raise_for_status(self):
            pass

    adapter = _AdapterQueGraba()
    session = OJVSession(adapter)
    session.csrf_token = "a" * 32

    await session.detail("civil", "jwt.de.mentira")

    assert adapter.data == {"dtaCausa": "jwt.de.mentira", "token": "a" * 32}


def test_la_falta_de_token_es_infra_y_no_de_la_causa():
    """El corazón del arreglo: sin esto el 405 salía como culpa de la causa."""
    assert classify_exception(MissingCsrfTokenError("no se pudo extraer")) == "infra"
