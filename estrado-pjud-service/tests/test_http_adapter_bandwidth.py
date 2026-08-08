from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.adapters.http_adapter import OJVHttpAdapter
from app.bandwidth import METER, capture_proxy_usage
from app.config import Settings


def _settings():
    return Settings(API_KEY="t", OJV_BASE_URL="https://x", RATE_LIMIT_MS=0, _env_file=None)


def _fake_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, request=httpx.Request("GET", "https://x/foo"))


@pytest.mark.asyncio
async def test_get_records_response_bytes_in_meter():
    METER.reset()
    adapter = OJVHttpAdapter(_settings())
    adapter._client.get = AsyncMock(return_value=_fake_response(b"0123456789"))

    await adapter.get("/foo")

    assert METER.total_bytes == 10


@pytest.mark.asyncio
async def test_post_records_response_bytes_in_meter():
    METER.reset()
    adapter = OJVHttpAdapter(_settings())
    adapter._client.post = AsyncMock(return_value=_fake_response(b"abcde"))

    await adapter.post("/foo")

    assert METER.total_bytes == 5


@pytest.mark.asyncio
async def test_get_accumulates_across_multiple_calls():
    METER.reset()
    adapter = OJVHttpAdapter(_settings())
    adapter._client.get = AsyncMock(return_value=_fake_response(b"12345"))

    await adapter.get("/foo")
    await adapter.get("/foo")

    assert METER.total_bytes == 10


@pytest.mark.asyncio
async def test_adapter_attributes_request_and_response_to_active_operation():
    adapter = OJVHttpAdapter(_settings())
    adapter._client.post = AsyncMock(return_value=httpx.Response(
        200,
        content=b"response",
        request=httpx.Request("POST", "https://x/foo", content=b"payload"),
    ))

    with capture_proxy_usage() as usage:
        await adapter.post("/foo", content=b"payload")

    assert usage.request_count == 1
    assert usage.bytes_up == len(b"payload")
    assert usage.bytes_down == len(b"response")


@pytest.mark.asyncio
async def test_adapter_retries_one_transient_transport_disconnect():
    adapter = OJVHttpAdapter(_settings())
    adapter._client.post = AsyncMock(side_effect=[
        httpx.RemoteProtocolError("server disconnected"),
        _fake_response(b"ok"),
    ])

    with capture_proxy_usage() as usage:
        response = await adapter.post("/foo", content=b"payload")

    assert response.content == b"ok"
    assert adapter._client.post.await_count == 2
    assert usage.request_count == 2
    assert usage.retry_count == 1


@pytest.mark.asyncio
async def test_adapter_stops_after_one_transient_transport_retry():
    adapter = OJVHttpAdapter(_settings())
    adapter._client.get = AsyncMock(side_effect=httpx.ConnectError("proxy down"))

    with capture_proxy_usage() as usage:
        with pytest.raises(httpx.ConnectError, match="proxy down"):
            await adapter.get("/foo")

    assert adapter._client.get.await_count == 2
    assert usage.request_count == 2
    assert usage.retry_count == 1


@pytest.mark.asyncio
async def test_post_once_never_retries_transport_errors():
    adapter = OJVHttpAdapter(_settings())
    adapter._client.post = AsyncMock(side_effect=httpx.ConnectError("proxy down"))

    with capture_proxy_usage() as usage:
        with pytest.raises(httpx.ConnectError, match="proxy down"):
            await adapter.post_once("/foo")

    assert adapter._client.post.await_count == 1
    assert usage.request_count == 1
    assert usage.retry_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_status", [307, 308])
async def test_post_once_never_follows_redirects_or_hides_wire_requests(
    redirect_status,
):
    wire_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        wire_requests.append(request)
        if request.url.path == "/redirected":
            return httpx.Response(200, content=b"unexpected", request=request)
        return httpx.Response(
            redirect_status,
            headers={"location": "/redirected"},
            content=b"redirect",
            request=request,
        )

    adapter = OJVHttpAdapter(_settings())
    await adapter._client.aclose()
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )

    try:
        with capture_proxy_usage() as usage:
            first = await adapter.post_once("/first")
            second = await adapter.post_once("/second")
    finally:
        await adapter.close()

    assert [response.status_code for response in (first, second)] == [
        redirect_status,
        redirect_status,
    ]
    assert len(wire_requests) == 2
    assert usage.request_count == 2
    assert usage.retry_count == 0
    assert usage.bytes_down == len(b"redirect") * 2
