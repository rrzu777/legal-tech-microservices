import logging
import time

import httpx
from fastapi import Request

from app.metrics import api_metrics

logger = logging.getLogger(__name__)


class TelegramAlerter:
    """Sends Telegram alerts when blocked rate exceeds threshold."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        blocked_rate_threshold: float = 0.3,
        cooldown_seconds: int = 300,
    ):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._threshold = blocked_rate_threshold
        self._cooldown = cooldown_seconds
        self._last_alert_time: float = 0.0
        self._client = httpx.AsyncClient(timeout=10.0)

    async def check_and_alert(self):
        """Check metrics and send alert if blocked rate exceeds threshold."""
        snapshot = api_metrics.snapshot()
        windowed_rate = api_metrics.windowed_blocked_rate()

        if snapshot["total_requests"] == 0:
            return

        if windowed_rate < self._threshold:
            return

        now = time.monotonic()
        if now - self._last_alert_time < self._cooldown:
            return

        self._last_alert_time = now

        msg = (
            "\u26a0\ufe0f PJUD blocked rate: {:.0%}\n"
            "Blocked: {}/{} requests\n"
            "Errors: {}\n"
            "Uptime: {}s"
        ).format(
            windowed_rate,
            snapshot["total_blocked"],
            snapshot["total_requests"],
            snapshot["total_errors"],
            snapshot["uptime_seconds"],
        )
        await self._send(msg)

    async def _send(self, text: str):
        """Send message via Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            resp = await self._client.post(url, json={
                "chat_id": self._chat_id,
                "text": text,
            })
            if resp.status_code != 200:
                logger.warning("Telegram alert failed: %s", resp.text)
        except Exception:
            logger.exception("Failed to send Telegram alert")

    async def close(self):
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()


async def maybe_alert(request: Request):
    """Trigger alert check if alerter is configured."""
    alerter = getattr(request.app.state, "alerter", None)
    if alerter:
        await alerter.check_and_alert()


async def send_ops_alert(bot_token: str, chat_id: str, event: str, detail: str) -> None:
    """Envia una alerta ops puntual a Telegram. No-op si falta token/chat.

    Nota: sin dedup/cooldown en esta fase (el pause del circuit breaker ya
    rate-limitea la frecuencia). El dedup completo llega en la Fase 2.
    """
    if not bot_token or not chat_id:
        return
    text = f"\U0001F6A8 [{event}] {detail}"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={"chat_id": chat_id, "text": text})
    except Exception:
        logger.exception("Fallo enviando ops alert")
