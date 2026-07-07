from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.adapters.http_adapter import OJVHttpAdapter
from app.bandwidth import METER
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
