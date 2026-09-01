"""Utilidades compartidas entre módulos de test."""

from unittest.mock import MagicMock


GENERATION_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GENERATION_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def runtime_control(*, generation=None, paused=False):
    return {
        "protocol_version": 1, "revision": 1,
        "admission_paused": paused, "generation_required": generation is not None,
        "generation": generation,
        "sealed_at": "2026-08-31T12:00:00+00:00" if generation else None,
        "bindings": dict.fromkeys(
            ("micro_sha", "web_sha", "rollback_micro_sha", "rollback_web_sha"), "a" * 40,
        ) if generation else None,
    }


class RuntimeControlDB:
    """Explicit in-memory read-only control RPC; never a global admission mock."""
    def __init__(self, control=None):
        self.control = runtime_control() if control is None else control
        self.calls = []
        self.error = None

    def rpc(self, name, args):
        assert name == "get_pjud_runtime_control"
        assert args == {}
        self.calls.append((name, args))
        return self

    def execute(self):
        from copy import deepcopy
        from types import SimpleNamespace
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=deepcopy(self.control))


def legacy_runtime_fence():
    from app.runtime_fence import RuntimeFence
    return RuntimeFence(RuntimeControlDB(), None)


def install_runtime_control(app):
    app.state.pjud_runtime_fence = legacy_runtime_fence()
    return app


def cookie_values(cookies) -> dict[str, str]:
    if isinstance(cookies, dict):
        return dict(cookies)
    return {cookie.name: cookie.value for cookie in (cookies or ())}


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


class AdapterQueGraba:
    """Adapter que anota cada request y devuelve `httpx.Response` de verdad.

    Graba en vez de levantar a propósito: afirmar por ausencia
    (`assert adapter.posts == []`) es lo que impide que el día que alguien saque
    una guardia el test siga pasando. Un fake que levanta `AssertionError` sale
    por el `pytest.raises` y deja el assert inalcanzable.

    Y `httpx.Response` de verdad, no un stub, para que los tests ejerciten el
    `raise_for_status()` y el `_decode()` reales de `OJVSession`.

    `posts` guarda `(path, kwargs)` —no sólo el path— porque hay tests que
    afirman sobre el cuerpo enviado. Estaba duplicada, con este mismo nombre y
    dos formas distintas de `posts`, en `test_detail_sin_csrf` y en
    `test_challenge_en_initialize`.
    """

    def __init__(self, *, html_get: str = "<html>ok</html>", cuerpo_post: bytes = b"<html>ok</html>"):
        self._html_get = html_get
        self._cuerpo_post = cuerpo_post
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    async def get(self, path, **kwargs):
        import httpx

        self.gets.append(path)
        return httpx.Response(
            200,
            content=self._html_get.encode("utf-8"),
            request=httpx.Request("GET", f"https://ojv.test{path}"),
        )

    async def post(self, path, **kwargs):
        import httpx

        self.posts.append((path, kwargs))
        return httpx.Response(
            200,
            content=self._cuerpo_post,
            request=httpx.Request("POST", f"https://ojv.test{path}"),
        )


def api_settings(
    store_path="/tmp/no-existe.json",
    *,
    proxy="http://u:p@residencial:9000",
    sticky_lifetime="1h",
):
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
        OJV_PROXY_STICKY_LIFETIME=sticky_lifetime,
        _env_file=None,
    )


def cookie_bundle(tag, *, proxy_url="http://u:p@sticky:1", age_seconds=0):
    """Un `CookieBundle` de test. Estaba escrito en tres archivos."""
    import time

    from app.cookie_store import CookieBundle

    return CookieBundle(
        cookies={"TSPD_101": f"tok-{tag}"},
        user_agent=f"UA-{tag}",
        saved_at=time.time() - age_seconds,
        proxy_url=proxy_url,
    )


def pool_con_store(
    monkeypatch,
    bundles,
    *,
    proxy="http://u:p@residencial:9000",
    sticky_lifetime="1h",
    session_cls=None,
):
    """`APISessionPool` con el store mockeado y el adapter/sesión falsos.

    Devuelve `(pool, capturados)`; `capturados` acumula los kwargs con los que se
    construyó cada adapter — que es donde se ve por qué IP salió cada intento, y
    si salió sin proxy.

    `session_cls` deja guionar `initialize()` (ver `_sesion_guionada` en
    `test_challenge_en_initialize`); por defecto la sesión muda de siempre.

    El patrón estaba copiado en cuatro archivos de test. Que sea uno solo importa
    porque la firma de `OJVHttpAdapter` y el nombre de `_store` son justo lo que
    cambia cuando se toca el pool.
    """
    from app import session_pool as sp
    from app.session_pool import APISessionPool

    pool = APISessionPool(
        api_settings(proxy=proxy, sticky_lifetime=sticky_lifetime),
        allow_uncontrolled_proxy=True,
    )
    pool._store = MagicMock()
    pool._store.load_all.return_value = bundles

    capturados = []

    def fake_adapter(s, proxy=None, user_agent=None, cookies=None):
        capturados.append({"proxy": proxy, "user_agent": user_agent, "cookies": cookies})
        return MagicMock()

    monkeypatch.setattr(sp, "OJVHttpAdapter", fake_adapter)
    monkeypatch.setattr(sp, "OJVSession", session_cls or FakeOJVSession)
    return pool, capturados


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
    from app.failure_kind import RejectedDetailSessionError

    return [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("refused"),
        httpx.ProxyError("proxy down"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
        RejectedDetailSessionError("405 vacío en detalle"),
    ]
