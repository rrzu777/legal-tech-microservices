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
        self._last_event_alert: dict[str, float] = {}
        self._client = httpx.AsyncClient(timeout=10.0)

    async def alert_event(self, event: str, detail: str) -> bool:
        """Alerta puntual por evento, con cooldown propio por tipo de evento.

        Existe porque `check_and_alert` NO PUEDE cubrir esto, y no por
        conveniencia: esa mira el RATIO de bloqueos sobre los requests
        registrados, y su primera guardia es `total_requests == 0`. Un fallo que
        ocurre antes de registrar el request —el pool que no entrega sesión— deja
        ese contador en cero para siempre, así que cuanto peor está el servicio,
        más callada queda esa vía. Una detección por proporción no puede ver su
        propio punto ciego: hace falta una señal que se dispare por el hecho.

        Pasó en producción: la API estuvo 3 días y 18 horas devolviendo 500 a
        todo, con `total_requests: 0` y `blocked_rate: 0.0`, y no salió una sola
        alerta. Nos enteramos porque un tester mandó una foto de la pantalla.

        Devuelve si mandó o si se lo comió el cooldown, para que el test lo pueda
        distinguir sin espiar atributos privados.
        """
        now = time.monotonic()
        last = self._last_event_alert.get(event)
        if last is not None and now - last < self._cooldown:
            return False

        self._last_event_alert[event] = now
        await self._send(ops_alert_text(event, detail))
        return True

    async def check_and_alert(self):
        """Check metrics and send alert if blocked rate exceeds threshold."""
        # El cooldown primero: es la guardia que descarta el 99% de las llamadas
        # y la unica que no cuesta nada —dos comparaciones de float, sin lock—.
        # Importa ahora que `classify_and_alert` llama a esto para TODO lo
        # clasificado "ojv" (403, 429, los 5xx) y no solo para dos tipos de
        # excepcion: bajo un outage sostenido del portal, evaluarlo al final
        # significaba una pasada completa a la ventana y dos tomas de lock por
        # request, para terminar descartando igual.
        now = time.monotonic()
        if now - self._last_alert_time < self._cooldown:
            return

        snapshot = api_metrics.snapshot()
        windowed_rate = api_metrics.windowed_blocked_rate()

        # La guardia se queda: sin requests no hay ratio que calcular y alertar
        # por un servicio ocioso seria ruido. Lo que no puede pasar es que esta
        # sea la UNICA via, porque el peor estado posible del servicio se ve
        # exactamente igual que estar ocioso. Esa cobertura la da `alert_event`.
        if snapshot["total_requests"] == 0:
            return

        if windowed_rate < self._threshold:
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


async def maybe_alert_event(request: Request, event: str, detail: str) -> None:
    """Alerta puntual, best-effort: avisar nunca puede empeorar el request.

    El `except` es la parte importante y no una formalidad. Los call sites llaman
    a esto en el camino de error, justo antes de un `raise` que preserva la
    excepción original; si esta corrutina lanzara, se llevaría puesta esa
    excepción y la reemplazaría por un fallo del alerter. La app dejaría de ver
    el error real y clasificaría mal la falla.
    """
    alerter = getattr(request.app.state, "alerter", None)
    if not alerter:
        return
    try:
        await alerter.alert_event(event, detail)
    except Exception:
        logger.exception("Fallo enviando alerta de evento %s", event)


def ops_alert_text(event: str, detail: str) -> str:
    """El formato de una alerta ops, en un solo lugar.

    Lo comparten `TelegramAlerter.alert_event` y `send_ops_alert`. Estaba escrito
    dos veces, y el precio de que se separen no es cosmético: ops grepea estas
    alertas como un conjunto, así que un prefijo de entorno o un hostname que se
    agregue en una sola de las dos vías parte ese conjunto en silencio.
    """
    return f"\U0001F6A8 [{event}] {detail}"


async def send_ops_alert(bot_token: str, chat_id: str, event: str, detail: str) -> None:
    """Envia una alerta ops puntual a Telegram. No-op si falta token/chat.

    Sin cooldown a propósito: sus tres call sites (parse-suspect en el engine,
    los dos de `worker/__main__`) ya rate-limitean por su cuenta. El dedup por
    tipo de evento —lo que este docstring llamaba "Fase 2"— existe y vive en
    `TelegramAlerter.alert_event`, que es la vía a usar cuando hay un `Request`
    a mano.
    """
    if not bot_token or not chat_id:
        return
    text = ops_alert_text(event, detail)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={"chat_id": chat_id, "text": text})
    except Exception:
        logger.exception("Fallo enviando ops alert")
