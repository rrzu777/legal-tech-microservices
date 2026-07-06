from unittest.mock import AsyncMock, patch
import pytest

from app.alerting import send_ops_alert


@pytest.mark.asyncio
async def test_send_ops_alert_posts_to_telegram():
    with patch("app.alerting.httpx.AsyncClient") as mock_client:
        instance = mock_client.return_value
        instance.post = AsyncMock()
        instance.aclose = AsyncMock()
        await send_ops_alert("token", "chat", "mint_failed", "minter caido")
        instance.post.assert_awaited_once()
        _, kwargs = instance.post.call_args
        assert "mint_failed" in kwargs["json"]["text"]
        assert kwargs["json"]["chat_id"] == "chat"


@pytest.mark.asyncio
async def test_send_ops_alert_noop_without_token():
    with patch("app.alerting.httpx.AsyncClient") as mock_client:
        await send_ops_alert("", "chat", "mint_failed", "x")
        mock_client.assert_not_called()
