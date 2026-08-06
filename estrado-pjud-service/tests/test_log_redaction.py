"""Secretos configurados nunca salen por los handlers de API ni worker."""

import io
import json
import logging

import httpx

from app.config import get_settings
from app.request_id import LOG_FORMAT, request_id_var


BOT_TOKEN = "123456789:FAKE-telegram-token-for-redaction-test"
SAFE_URL = "https://api.telegram.org/bot[REDACTED]/sendMessage"


def _api_log_output(monkeypatch, msg, args=()) -> str:
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
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
        logging.getLogger("httpx").info(msg, *args)
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
