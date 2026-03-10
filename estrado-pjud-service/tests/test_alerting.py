"""Tests for Telegram alerting."""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()


class TestTelegramAlerter:
    @pytest.mark.asyncio
    async def test_alert_fires_when_blocked_rate_exceeds_threshold(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(4):
            api_metrics.record_blocked("search")

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            await alerter.check_and_alert()
            mock_send.assert_awaited_once()
            assert "blocked" in mock_send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_alert_does_not_fire_below_threshold(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        for _ in range(10):
            api_metrics.record_request("search")
        api_metrics.record_blocked("search")  # 10% < 30%

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            await alerter.check_and_alert()
            mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alert_respects_cooldown(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(5):
            api_metrics.record_blocked("search")

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            await alerter.check_and_alert()
            await alerter.check_and_alert()
            assert mock_send.await_count == 1

    @pytest.mark.asyncio
    async def test_no_alert_when_no_requests(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            await alerter.check_and_alert()
            mock_send.assert_not_awaited()
