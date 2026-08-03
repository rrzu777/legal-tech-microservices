"""El X-Request-ID de la app viaja al log y vuelve en la respuesta.

La correlación app↔micro es el punto: cada incidente se reconstruía cruzando
logs de Vercel con el journal a mano por timestamp. Lo que se pinnea acá:

- header válido → se usa TAL CUAL (si acuñáramos otro, la correlación muere)
- header ausente o inválido → se acuña uno (el request necesita identidad
  igual), y el inválido JAMÁS llega al log: va derecho a la línea de journal
  y un valor con \\n inyectaría entradas falsas
- el rid vuelve en la respuesta y está disponible vía contextvar DENTRO del
  request (que es lo que el filter estampa en cada línea)
- fuera de un request el contextvar vuelve a "-" (los logs del arranque no
  heredan el rid del último request atendido)
"""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.request_id import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    normalize_request_id,
    request_id_middleware,
    request_id_var,
)

app = FastAPI()
app.middleware("http")(request_id_middleware)


@app.get("/echo")
async def echo():
    return {"rid": request_id_var.get()}


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


class TestCableado:
    """Contra create_app() REAL: que el middleware esté registrado de verdad.

    Los tests de arriba usan una mini-app; si alguien borrara la línea de
    app.middleware() en main.py, seguirían verdes. Éste no.
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

    def test_fuera_de_request_el_record_lleva_guion(self):
        record = logging.LogRecord("app.x", logging.INFO, "f.py", 1, "msg", None, None)
        RequestIdFilter().filter(record)
        assert record.rid == "-"

    def test_el_formato_del_main_renderiza(self):
        # El format de create_app usa %(rid)s: si el filter no corriera, el
        # handler explotaría con KeyError en cada línea. Se prueba el par.
        record = logging.LogRecord("app.x", logging.INFO, "f.py", 1, "hola", None, None)
        RequestIdFilter().filter(record)
        out = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s [rid=%(rid)s]: %(message)s"
        ).format(record)
        assert "[rid=-]: hola" in out

    def test_una_linea_emitida_dentro_del_request_lleva_el_rid(self, caplog):
        caplog.handler.addFilter(RequestIdFilter())

        logged = logging.getLogger("app.test_request_id")

        @app.get("/loguea")
        async def loguea():
            logged.info("adentro")
            return {}

        try:
            with caplog.at_level(logging.INFO, logger="app.test_request_id"):
                client.get("/loguea", headers={REQUEST_ID_HEADER: "rid-visible-en-log"})
        finally:
            caplog.handler.removeFilter(caplog.handler.filters[-1])

        rids = [r.rid for r in caplog.records if r.name == "app.test_request_id"]
        assert rids == ["rid-visible-en-log"]
