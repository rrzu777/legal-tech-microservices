from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.bandwidth import METER
from worker.__main__ import BandwidthAlertState, maybe_alert_bandwidth


def _config(**overrides):
    defaults = dict(
        OJV_PROXY_URL="http://proxy:8080",
        OJV_PROXY_GB_BUDGET=2.0,
        OJV_PROXY_GB_ALERT_PCT=80,
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="chat",
        WORKER_ID="worker-1",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _reset_meter():
    METER.reset()
    yield
    METER.reset()


@pytest.mark.asyncio
async def test_below_threshold_does_not_alert():
    METER.add(int(1.0 * 1024 ** 3))  # 1.0GB of 2.0GB budget, 80% threshold => need 1.6GB
    config = _config()
    state = BandwidthAlertState()

    with patch("worker.__main__.send_ops_alert", new=AsyncMock()) as mock_alert:
        await maybe_alert_bandwidth(config, state, now=0.0)

    mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_at_or_above_threshold_alerts_once():
    METER.add(int(1.7 * 1024 ** 3))  # 1.7GB >= 80% of 2.0GB (1.6GB)
    config = _config()
    state = BandwidthAlertState()

    with patch("worker.__main__.send_ops_alert", new=AsyncMock()) as mock_alert:
        await maybe_alert_bandwidth(config, state, now=0.0)

    mock_alert.assert_awaited_once()
    args, kwargs = mock_alert.call_args
    assert args[0] == "token"
    assert args[1] == "chat"
    assert args[2] == "bandwidth_high"
    assert "GB" in args[3]


@pytest.mark.asyncio
async def test_within_cooldown_does_not_alert_again():
    METER.add(int(1.9 * 1024 ** 3))
    config = _config()
    state = BandwidthAlertState()

    with patch("worker.__main__.send_ops_alert", new=AsyncMock()) as mock_alert:
        await maybe_alert_bandwidth(config, state, now=0.0)
        await maybe_alert_bandwidth(config, state, now=100.0)  # well within 6h cooldown

    mock_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_alerts_again_after_cooldown_expires():
    METER.add(int(1.9 * 1024 ** 3))
    config = _config()
    state = BandwidthAlertState()

    six_hours = 6 * 60 * 60
    with patch("worker.__main__.send_ops_alert", new=AsyncMock()) as mock_alert:
        await maybe_alert_bandwidth(config, state, now=0.0)
        await maybe_alert_bandwidth(config, state, now=six_hours + 1)

    assert mock_alert.await_count == 2


@pytest.mark.asyncio
async def test_no_proxy_mode_never_alerts():
    METER.add(int(5.0 * 1024 ** 3))  # way above any budget
    config = _config(OJV_PROXY_URL=None)
    state = BandwidthAlertState()

    with patch("worker.__main__.send_ops_alert", new=AsyncMock()) as mock_alert:
        await maybe_alert_bandwidth(config, state, now=0.0)

    mock_alert.assert_not_called()
