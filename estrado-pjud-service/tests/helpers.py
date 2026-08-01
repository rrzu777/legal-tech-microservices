"""Utilidades compartidas entre módulos de test."""


class FakeOJVSession:
    """Sesión que no habla con nadie. Estaba copiada en cinco archivos.

    Espeja la superficie de `OJVSession` que el pool usa, y NADA más: un fake con
    atributos que la clase real no tiene es lo que deja pasar un test contra algo
    que no existe.
    """

    def __init__(self, adapter=None):
        self.adapter = adapter
        self.closed = False

    async def initialize(self):
        pass

    async def close(self):
        self.closed = True

    @property
    def age_seconds(self):
        return 0.0


def api_settings(store_path="/tmp/no-existe.json", *, proxy="http://u:p@residencial:9000"):
    """`Settings` de verdad para construir un `APISessionPool`, nunca un MagicMock.

    `APISessionPool.__init__` mira `OJV_PROXY_URL` para saber si está en modo
    proxy. Con un MagicMock ese atributo es un Mock auto-generado —o sea, no
    `None`— y el modo proxy queda activado POR ACCIDENTE: el test pasa a medir el
    mock en vez del código.

    `proxy=None` da el modo legacy, donde salir directo es lo previsto.
    """
    from app.config import Settings

    return Settings(
        API_KEY="t",
        COOKIE_STORE_PATH=str(store_path),
        OJV_PROXY_URL=proxy,
        _env_file=None,
    )


def find_update_payload(mock_sb, **match):
    """Devuelve el primer payload de .update() que matchea todos los pares clave/valor.

    Los tests inspeccionan el dict que el engine manda a Supabase; el mock encadena
    from_().update(), así que hay que barrer call_args_list y filtrar por contenido.
    """
    for call in mock_sb.from_.return_value.update.call_args_list:
        args = call[0] if call[0] else ()
        kwargs = call[1] if call[1] else {}
        payload = args[0] if args else kwargs.get("data")
        if payload and all(payload.get(k) == v for k, v in match.items()):
            return payload
    return None


def http_status_error(code: int) -> "httpx.HTTPStatusError":
    """Un `HTTPStatusError` de OJV con el status pedido.

    Lo arman cinco tests entre `test_failure_kind`, `test_engine` y
    `test_routes`, con tres argumentos que ninguno de ellos quiere describir. En
    la PR que convierte el status HTTP en el eje de la clasificacion, es el
    fixture que mas se va a volver a necesitar.
    """
    import httpx

    return httpx.HTTPStatusError(
        str(code),
        request=httpx.Request("POST", "https://ojv.test"),
        response=httpx.Response(code),
    )


def infra_exceptions() -> list:
    """Las excepciones que significan "se cayo NUESTRO lado".

    Una sola lista: estaba copiada en tres parametrize, asi que agregar un tipo
    al clasificador exigia acordarse de tres lugares — y el que se olvidara no
    fallaba, simplemente dejaba de estar testeado. Los tres ultimos son los que
    la lista vieja de las rutas dejaba afuera, y son justo lo que produce un
    pool de IPs residenciales.
    """
    import httpx

    return [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("refused"),
        httpx.ProxyError("proxy down"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
    ]
