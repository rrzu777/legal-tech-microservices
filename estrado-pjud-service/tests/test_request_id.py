"""El X-Request-ID de la app viaja al log y vuelve en la respuesta.

La correlación app↔micro es el punto: cada incidente se reconstruía cruzando
logs de Vercel con el journal a mano por timestamp. Lo que se pinnea acá:

- header válido → se usa TAL CUAL (si acuñáramos otro, la correlación muere)
- header ausente o inválido → se acuña uno (el request necesita identidad
  igual), y el inválido JAMÁS llega al log: va derecho a la línea de journal
  y un valor con \\n inyectaría entradas falsas
- el rid vuelve en la respuesta, está en el contextvar DENTRO del request
  (que es lo que el filter estampa en cada línea), y se limpia al salir
- create_app() REAL registra el middleware (mini-app no basta para eso)
"""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.request_id import (
    LOG_FORMAT,
    REQUEST_ID_HEADER,
    RequestIdFilter,
    RequestIdMiddleware,
    normalize_request_id,
    request_id_var,
)

app = FastAPI()

logged = logging.getLogger("app.test_request_id")


@app.get("/echo")
async def echo():
    return {"rid": request_id_var.get()}


@app.get("/loguea")
async def loguea():
    logged.info("adentro")
    return {}


app.add_middleware(RequestIdMiddleware)
client = TestClient(app)


class TestNormalize:
    def test_valido_pasa_tal_cual(self):
        assert normalize_request_id("abc12345-def.67890") == "abc12345-def.67890"

    def test_ausente_acuna_uno(self):
        rid = normalize_request_id(None)
        assert len(rid) == 32
        assert rid != normalize_request_id(None)  # distintos por request

    def test_invalidos_no_pasan(self):
        # Inyección de log, demasiado corto, demasiado largo, caracteres raros.
        for raw in ["x\n[ERROR] fake", "abc", "a" * 65, "abc 12345", "ñoño12345"]:
            assert normalize_request_id(raw) != raw


class TestMiddleware:
    def test_header_valido_se_usa_y_vuelve(self):
        resp = client.get("/echo", headers={REQUEST_ID_HEADER: "rid-de-la-app-123"})
        assert resp.json()["rid"] == "rid-de-la-app-123"
        assert resp.headers[REQUEST_ID_HEADER] == "rid-de-la-app-123"

    def test_sin_header_acuna_y_lo_devuelve(self):
        resp = client.get("/echo")
        rid = resp.headers[REQUEST_ID_HEADER]
        assert len(rid) == 32
        assert resp.json()["rid"] == rid

    def test_header_con_inyeccion_no_llega_al_contexto(self):
        resp = client.get("/echo", headers={REQUEST_ID_HEADER: "x-y-z"})  # corto
        assert resp.json()["rid"] != "x-y-z"

    def test_fuera_del_request_vuelve_al_default(self):
        client.get("/echo", headers={REQUEST_ID_HEADER: "rid-que-no-debe-quedar"})
        assert request_id_var.get() == "-"

    def test_una_capa_asgi_exterior_no_ve_el_rid_tras_la_respuesta(self):
        # El observador real del reset: una capa ASGI MÁS exterior corre en la
        # misma task (el middleware es ASGI puro, sin task por capa), así que
        # su código post-respuesta vería el rid del request sin el finally.
        visto: dict[str, str] = {}

        class Sonda:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                await self.app(scope, receive, send)
                visto["despues"] = request_id_var.get()

        exterior = FastAPI()

        @exterior.get("/x")
        async def x():
            return {"rid": request_id_var.get()}

        exterior.add_middleware(RequestIdMiddleware)
        exterior.add_middleware(Sonda)  # agregado después = más exterior

        resp = TestClient(exterior).get("/x", headers={REQUEST_ID_HEADER: "rid-interior-1"})
        assert resp.json()["rid"] == "rid-interior-1"  # adentro sí se vio
        assert visto["despues"] == "-"  # afuera ya no


class TestCableado:
    """Contra create_app() REAL: que el middleware esté registrado de verdad.

    Los tests de arriba usan una mini-app; si alguien borrara la línea de
    add_middleware en main.py, seguirían verdes. Éste no.
    """

    def test_health_devuelve_el_request_id(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "test-secret")
        from app.config import get_settings

        get_settings.cache_clear()
        from app.main import create_app

        # Sin lifespan a propósito: /api/v1/health no toca el session pool.
        real = TestClient(create_app())
        resp = real.get("/api/v1/health", headers={REQUEST_ID_HEADER: "rid-cableado-ok"})
        assert resp.status_code == 200
        assert resp.headers[REQUEST_ID_HEADER] == "rid-cableado-ok"


class TestFilter:
    def test_estampa_el_contextvar_en_el_record(self):
        record = logging.LogRecord("app.x", logging.INFO, "f.py", 1, "msg", None, None)
        token = request_id_var.set("rid-en-curso-123")
        try:
            assert RequestIdFilter().filter(record) is True
        finally:
            request_id_var.reset(token)
        assert record.rid == "rid-en-curso-123"

    def test_el_formato_del_main_renderiza(self):
        # LOG_FORMAT usa %(rid)s: si el filter no corriera, el handler
        # explotaría con KeyError en cada línea. Se prueba el par real
        # (el formato se importa, no se copia — copiado seguiría verde
        # aunque el de producción driftara).
        record = logging.LogRecord("app.x", logging.INFO, "f.py", 1, "hola", None, None)
        RequestIdFilter().filter(record)
        out = logging.Formatter(LOG_FORMAT).format(record)
        assert "[rid=-]: hola" in out

    def test_una_linea_emitida_dentro_del_request_lleva_el_rid(self, caplog):
        filtro = RequestIdFilter()
        caplog.handler.addFilter(filtro)
        try:
            with caplog.at_level(logging.INFO, logger="app.test_request_id"):
                client.get("/loguea", headers={REQUEST_ID_HEADER: "rid-visible-en-log"})
        finally:
            caplog.handler.removeFilter(filtro)

        rids = [r.rid for r in caplog.records if r.name == "app.test_request_id"]
        assert rids == ["rid-visible-en-log"]
