"""/docs, /redoc y /openapi.json cerrados salvo flag explícito.

FastAPI monta esas tres rutas por su cuenta y NO les aplica las
``dependencies`` de los ``include_router``, así que estuvieron abiertas
publicando el contrato completo del servicio privado de credenciales: cada
endpoint, cada schema y los nombres de las cabeceras ``X-JurisTrack-*``. Con el
puerto 8000 todavía expuesto a internet (ver ops/caddy/Caddyfile) eso es
reconocimiento gratis.

Se afirma sobre ``create_app()`` y no con un TestClient a propósito: entrar al
lifespan arma el pool de sesiones y los clientes de Supabase, que no tienen
nada que ver con lo que esta guarda mide.
"""

import pytest

DOC_PATHS = {"/docs", "/redoc", "/openapi.json"}


def build_app(monkeypatch, *, docs: str | None):
    monkeypatch.setenv("API_KEY", "test-secret")
    if docs is None:
        monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    else:
        monkeypatch.setenv("ENABLE_API_DOCS", docs)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    try:
        return create_app()
    finally:
        get_settings.cache_clear()


def mounted_paths(app) -> set[str]:
    return {getattr(route, "path", None) for route in app.routes}


@pytest.mark.parametrize("docs", [None, "false", "TRUE", "1"])
def test_docs_cerradas_por_default_y_ante_cualquier_valor_que_no_sea_true(monkeypatch, docs):
    # El flag es igualdad exacta con "true", igual que el resto de los ENABLE_*
    # de este repo: "TRUE" o "1" no alcanzan para abrir el contrato.
    app = build_app(monkeypatch, docs=docs)
    assert mounted_paths(app) & DOC_PATHS == set()
    assert app.openapi_url is None


def test_docs_vuelven_con_el_flag_explicito(monkeypatch):
    # Verificación por mutación de la guarda de arriba: si el flag no hiciera
    # nada, este test pasaría igual y el otro sería un falso positivo.
    app = build_app(monkeypatch, docs="true")
    assert DOC_PATHS <= mounted_paths(app)
    assert app.openapi_url == "/openapi.json"
