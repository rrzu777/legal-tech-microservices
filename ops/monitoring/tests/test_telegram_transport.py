import io
import json
import urllib.error
import urllib.request

import pytest

from ops.monitoring.monitor import TelegramDeliveryError, TelegramTransport


TOKEN = "123456:bot-secret-material"
CHAT_ID = "-987654321"
MESSAGE = "payload-secret"


class Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


def test_transport_posts_json_with_timeout_and_requires_telegram_ok():
    captured = {}
    response = Response(200, b'{"ok": true, "result": {"message_id": 1}}')

    def opener(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    TelegramTransport(TOKEN, CHAT_ID, opener, timeout=2.5).send("safe message")

    request = captured["request"]
    assert request.method == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"chat_id": CHAT_ID, "text": "safe message"}
    assert captured["timeout"] == 2.5
    assert response.closed is True


@pytest.mark.parametrize(
    "response",
    [
        Response(400, b'{"ok": true}'),
        Response(500, b'{"ok": true}'),
        Response(200, b"not-json"),
        Response(200, b'{"ok": false, "description": "payload-secret"}'),
    ],
)
def test_transport_rejects_bad_http_or_payload_without_disclosing_secrets(response):
    transport = TelegramTransport(TOKEN, CHAT_ID, lambda request, timeout: response)

    with pytest.raises(TelegramDeliveryError) as caught:
        transport.send(MESSAGE)

    rendered = str(caught.value)
    assert rendered == "Telegram delivery failed"
    assert TOKEN not in rendered
    assert CHAT_ID not in rendered
    assert MESSAGE not in rendered
    assert "api.telegram.org" not in rendered
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("payload-secret"),
        urllib.error.URLError(f"timeout {TOKEN} {CHAT_ID} {MESSAGE}"),
        urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            401,
            f"denied {CHAT_ID} {MESSAGE}",
            {},
            io.BytesIO(b"payload-secret"),
        ),
    ],
)
def test_transport_sanitizes_network_exceptions(error):
    def opener(request, timeout):
        raise error

    with pytest.raises(TelegramDeliveryError) as caught:
        TelegramTransport(TOKEN, CHAT_ID, opener).send(MESSAGE)

    rendered = str(caught.value)
    assert rendered == "Telegram delivery failed"
    assert TOKEN not in rendered
    assert CHAT_ID not in rendered
    assert MESSAGE not in rendered
    assert "api.telegram.org" not in rendered
    assert caught.value.__cause__ is None


def test_transport_sanitizes_request_construction_errors(monkeypatch):
    def fail_request(url, **kwargs):
        raise ValueError(f"bad request {url} {MESSAGE}")

    monkeypatch.setattr(urllib.request, "Request", fail_request)

    with pytest.raises(TelegramDeliveryError) as caught:
        TelegramTransport(TOKEN, CHAT_ID, lambda request, timeout: None).send(MESSAGE)

    assert str(caught.value) == "Telegram delivery failed"
    assert TOKEN not in str(caught.value)
    assert MESSAGE not in str(caught.value)
    assert caught.value.__cause__ is None


def test_transport_sanitizes_response_close_errors():
    class CloseFailureResponse(Response):
        def close(self):
            raise RuntimeError(f"close failed {TOKEN} {CHAT_ID} {MESSAGE}")

    response = CloseFailureResponse(200, b'{"ok": true}')

    with pytest.raises(TelegramDeliveryError) as caught:
        TelegramTransport(TOKEN, CHAT_ID, lambda request, timeout: response).send(
            MESSAGE
        )

    assert str(caught.value) == "Telegram delivery failed"
    assert TOKEN not in str(caught.value)
    assert CHAT_ID not in str(caught.value)
    assert MESSAGE not in str(caught.value)
    assert caught.value.__cause__ is None
