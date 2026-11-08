"""Secretos configurados nunca salen por los handlers de API ni worker."""

import io
import json
import logging

import httpx
import pytest

from types import SimpleNamespace

from app.config import get_settings
from app.request_id import LOG_FORMAT, request_id_var


BOT_TOKEN = "123456789:FAKE-telegram-token-for-redaction-test"
SAFE_URL = "https://api.telegram.org/bot[REDACTED]/sendMessage"


def _api_log_output(monkeypatch, msg, args=(), *, env=None, logger="httpx") -> str:
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    token = request_id_var.set("rid-redaction-123")
    try:
        from app.main import create_app

        create_app()
        logging.getLogger(logger).info(msg, *args)
    finally:
        request_id_var.reset(token)
        root.removeHandler(handler)
        root.setLevel(old_level)
        get_settings.cache_clear()
    return stream.getvalue()


def test_api_redacta_secreto_en_args_sin_perder_url_metodo_ni_rid(monkeypatch):
    output = _api_log_output(
        monkeypatch,
        'HTTP Request: %s %s "%s %d %s"',
        (
            "POST",
            httpx.URL(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"),
            "HTTP/1.1",
            200,
            "OK",
        ),
    )

    assert BOT_TOKEN not in output
    assert f"POST {SAFE_URL}" in output
    assert "[rid=rid-redaction-123]" in output


def test_api_redacta_secreto_en_msg_ya_formateado(monkeypatch):
    output = _api_log_output(
        monkeypatch,
        f"HTTP Request: POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage 200 OK",
    )

    assert BOT_TOKEN not in output
    assert f"POST {SAFE_URL} 200 OK" in output


def test_api_redacta_tokens_de_descarga_pjud_sin_ocultar_endpoint(monkeypatch):
    document_token = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    output = _api_log_output(
        monkeypatch,
        'HTTP Request: %s %s "%s %d %s"',
        (
            "GET",
            httpx.URL(
                "https://oficinajudicialvirtual.pjud.cl/ADIR_871/civil/"
                f"documentos/docCertificadoEscrito.php?dtaCert={document_token}"
                "&modo=publico"
            ),
            "HTTP/1.1",
            200,
            "OK",
        ),
    )

    assert document_token not in output
    assert (
        "GET https://oficinajudicialvirtual.pjud.cl/ADIR_871/civil/"
        "documentos/docCertificadoEscrito.php?dtaCert=[REDACTED]&modo=publico"
        in output
    )


@pytest.mark.parametrize("param_name", ["dtaDoc", "valorDoc", "valorFile"])
def test_api_redacta_cada_parametro_de_documento_pjud(monkeypatch, param_name):
    document_token = "token-de-descarga-que-no-debe-salir"
    output = _api_log_output(
        monkeypatch,
        "HTTP Request: GET %s",
        (
            httpx.URL(
                "https://oficinajudicialvirtual.pjud.cl/documentos/archivo.php"
                f"?{param_name}={document_token}"
            ),
        ),
    )

    assert document_token not in output
    assert f"?{param_name}=[REDACTED]" in output


def test_worker_redacta_sin_romper_el_json(monkeypatch):
    from worker.__main__ import setup_logging

    stream = io.StringIO()
    monkeypatch.setattr("worker.__main__.sys.stdout", stream)
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    try:
        setup_logging("INFO", secrets=(BOT_TOKEN,))
        logging.getLogger("httpx").info(
            "HTTP Request: %s %s",
            "POST",
            httpx.URL(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"),
        )
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)

    payload = json.loads(stream.getvalue())
    assert BOT_TOKEN not in payload["msg"]
    assert payload["msg"] == f"HTTP Request: POST {SAFE_URL}"


def test_worker_redacta_parametro_pjud_con_escapes_y_conserva_json(monkeypatch):
    from worker.__main__ import setup_logging

    stream = io.StringIO()
    monkeypatch.setattr("worker.__main__.sys.stdout", stream)
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    try:
        setup_logging("INFO")
        logging.getLogger("httpx").info(
            'HTTP Request: GET https://pjud.example/doc?dtaDoc=abc"def\\ghi&modo=publico'
        )
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)

    payload = json.loads(stream.getvalue())
    assert payload["msg"] == (
        "HTTP Request: GET https://pjud.example/doc?dtaDoc=[REDACTED]&modo=publico"
    )


API_KEY_VALUE = "api-key-7f3a9c2e-never-in-the-journal"
INTERNAL_KEY_VALUE = "internal-credentials-key-4b8d1e-never-in-the-journal"
CAPABILITY_VALUE = "c" * 64


def test_api_redacta_las_dos_llaves_de_autenticacion(monkeypatch):
    """Las dos API keys del servicio también se redactan.

    Faltaban en la tupla de secretos, que solo llevaba Telegram, la service key
    de Supabase y la URL del proxy. API_KEY abre todo el servicio e
    INTERNAL_CREDENTIALS_API_KEY abre el endpoint que devuelve contraseñas PJUD
    descifradas; las dos son `str` y no `SecretStr`, así que cualquier
    repr(settings) o traceback que arrastre la config las escribía en claro al
    journal, que está menos protegido que el .env.
    """
    output = _api_log_output(
        monkeypatch,
        "settings dump: api=%s internal=%s",
        (API_KEY_VALUE, INTERNAL_KEY_VALUE),
        env={"API_KEY": API_KEY_VALUE, "INTERNAL_CREDENTIALS_API_KEY": INTERNAL_KEY_VALUE},
        logger="app.test",
    )
    assert API_KEY_VALUE not in output
    assert INTERNAL_KEY_VALUE not in output
    # La línea sigue siendo legible y conserva el rid: se redacta el valor, no
    # el log entero.
    assert "settings dump: api=[REDACTED] internal=[REDACTED]" in output
    assert "rid-redaction-123" in output


def test_handler_del_worker_redacta_lo_que_secret_values_entrega(monkeypatch):
    """Afirma el handler del worker, NO el cableado de su `main()`.

    Este test le pasa los secretos a mano, así que prueba dos cosas: que
    `secret_values` incluye la llave del canal de credenciales, y que el handler
    del worker redacta lo que recibe. Lo que NO prueba es que `worker/__main__.py`
    llame a `secret_values`, y hoy no lo llama: ese archivo es uno de los 17 de
    `wm_contract_paths`, `ops/deploy.sh` lo congela, y el cambio viaja en una
    operación autorizada aparte (ver tests/test_config.py).

    Mientras eso no se despliegue, el worker sigue redactando su lista vieja
    —Telegram, service key de Supabase, URL del proxy y la capability— y por lo
    tanto **no** redacta INTERNAL_CREDENTIALS_API_KEY, que es la llave con la que
    ese mismo proceso pide la contraseña PJUD descifrada. No se arregla acá a
    propósito: un test no puede tapar un archivo congelado, y escribir el nombre
    de este test como si ya estuviera cableado es cómo se perdieron las llaves la
    primera vez.
    """
    from app.logging_redaction import secret_values
    from worker.__main__ import setup_logging

    config = SimpleNamespace(
        INTERNAL_CREDENTIALS_API_KEY=INTERNAL_KEY_VALUE,
        SUPABASE_SERVICE_KEY="",
        TELEGRAM_BOT_TOKEN=BOT_TOKEN,
        OJV_PROXY_URL=None,
    )

    stream = io.StringIO()
    monkeypatch.setattr("worker.__main__.sys.stdout", stream)
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    try:
        setup_logging("INFO", secrets=secret_values(config))
        logging.getLogger("app.test").info("claim a %s", INTERNAL_KEY_VALUE)
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)

    payload = json.loads(stream.getvalue())
    assert INTERNAL_KEY_VALUE not in payload["msg"]
    assert payload["msg"] == "claim a [REDACTED]"


def test_secret_values_desenvuelve_secretstr_y_tolera_campos_ausentes():
    """Dos trampas que harían que la redacción fallara pareciendo funcionar.

    Si un campo pasa a `SecretStr` y nadie desenvuelve, lo que se redacta es la
    máscara y el secreto real sigue saliendo en claro. Y WorkerConfig no declara
    API_KEY: una lista por nombre tiene que absorber esa asimetría en vez de
    bifurcarse en dos listas, que es como se perdieron las llaves.
    """
    from pydantic import SecretStr

    from app.logging_redaction import SECRET_CONFIG_FIELDS, secret_values

    assert "API_KEY" in SECRET_CONFIG_FIELDS
    assert "INTERNAL_CREDENTIALS_API_KEY" in SECRET_CONFIG_FIELDS

    values = secret_values(
        SimpleNamespace(
            INTERNAL_CREDENTIALS_API_KEY=SecretStr(INTERNAL_KEY_VALUE),
            TELEGRAM_BOT_TOKEN="",
            OJV_PROXY_URL=None,
        ),
        CAPABILITY_VALUE,
        None,
    )
    assert values == (INTERNAL_KEY_VALUE, CAPABILITY_VALUE)
    assert "**********" not in values
