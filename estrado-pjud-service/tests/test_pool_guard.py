"""El punto ciego del alerter, escrito como test.

El 31 de julio de 2026 la API estuvo 3 días y 18 horas devolviendo 500 a todas
las consultas y `/api/v1/health` respondía:

    {"status": "ok", "total_requests": 0, "total_errors": 0, "blocked_rate": 0.0}

Un servicio totalmente caído, indistinguible de uno ocioso, sin una sola alerta.
La causa: `SessionPool.acquire()` lanzaba antes de `record_request()`, así que
ningún contador se movía, y la única vía de alerta —`check_and_alert`, que mira
el RATIO de bloqueos— sale temprano justo cuando `total_requests == 0`. Cuanto
peor estaba el servicio, más callado.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.alerting import TelegramAlerter
from app.failure_kind import PoolUnavailableError
from app.metrics import api_metrics
from app.pool_guard import (
    POOL_UNAVAILABLE_EVENT,
    PUBLIC_POOL_UNAVAILABLE_DETAIL,
    acquire_or_alert,
)


class _BrokenPool:
    """El pool que no puede entregar sesión: sin bundle F5, el initialize revienta."""

    async def acquire(self):
        raise RuntimeError("Connection refused a ojv.pjud.cl")


class _WorkingPool:
    async def acquire(self):
        return "session"


class _OperationalBrokenPool:
    async def acquire(self):
        raise PoolUnavailableError("mint_exhausted")


class _BuggyPool:
    def __init__(self, error):
        self.error = error

    async def acquire(self):
        raise self.error


def _request_with_alerter(alerter):
    request = MagicMock()
    request.app.state.alerter = alerter
    return request


@pytest.mark.asyncio
async def test_devuelve_la_sesion_cuando_el_pool_anda():
    session = await acquire_or_alert(_WorkingPool(), _request_with_alerter(None), "search")

    assert session == "session"
    # El camino feliz no toca el contador de fallos: si lo tocara, el watchdog
    # externo vería fallos de pool en un servicio sano.
    assert api_metrics.snapshot()["total_pool_failures"] == 0


@pytest.mark.asyncio
async def test_relanza_para_que_la_app_lo_clasifique_como_infra():
    # NO se traga la excepción devolviendo `blocked=True`: eso haría que la app
    # tratara una caída nuestra como un bloqueo de OJV, que es la atribución
    # falsa que este trabajo vino a cerrar. El 5xx es lo que la app convierte en
    # `PjudInfraError`, y por eso NO le suma fallas a la causa.
    with pytest.raises(RuntimeError, match="Connection refused"):
        await acquire_or_alert(_BrokenPool(), _request_with_alerter(None), "search")


@pytest.mark.asyncio
async def test_operational_pool_failure_is_a_safe_503_and_alert_code():
    """Removing the typed 503 boundary would expose the acquisition failure."""
    alerter = TelegramAlerter("token", "chat")
    alerter._send = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await acquire_or_alert(_OperationalBrokenPool(), _request_with_alerter(alerter), "search")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == PUBLIC_POOL_UNAVAILABLE_DETAIL
    alert = alerter._send.await_args.args[0]
    assert "pool_failure=mint_exhausted" in alert
    assert "mint_exhausted" not in exc_info.value.detail


@pytest.mark.asyncio
async def test_unexpected_pool_exception_preserves_identity_and_uses_safe_alert_code():
    """A catch-all 503 mapping would hide programming defects from the API."""
    sentinel = RuntimeError("programming invariant sentinel")
    alerter = TelegramAlerter("token", "chat")
    alerter._send = AsyncMock()

    with pytest.raises(RuntimeError) as exc_info:
        await acquire_or_alert(_BuggyPool(sentinel), _request_with_alerter(alerter), "detail")

    assert exc_info.value is sentinel
    alert = alerter._send.await_args.args[0]
    assert "pool_failure=unexpected_exception" in alert
    assert "programming invariant sentinel" not in alert


@pytest.mark.asyncio
async def test_el_fallo_de_pool_deja_rastro_en_las_metricas():
    # Esta es la métrica que hace visible el escenario: el resto se incrementa
    # más adelante en el request, así que acá quedan todos en cero.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await acquire_or_alert(_BrokenPool(), _request_with_alerter(None), "search")

    snapshot = api_metrics.snapshot()
    assert snapshot["total_pool_failures"] == 2
    assert snapshot["total_requests"] == 0
    # Y NO se cuenta como bloqueo: OJV no tuvo nada que ver.
    assert snapshot["total_blocked"] == 0


@pytest.mark.asyncio
async def test_alerta_aunque_el_ratio_de_bloqueos_este_ciego():
    alerter = TelegramAlerter("token", "chat")
    alerter._send = AsyncMock()

    with pytest.raises(RuntimeError):
        await acquire_or_alert(_BrokenPool(), _request_with_alerter(alerter), "search")

    alerter._send.assert_awaited_once()
    text = alerter._send.await_args.args[0]
    assert POOL_UNAVAILABLE_EVENT in text
    assert "search" in text

    # La contraprueba, y es el corazón del bug: la vía de siempre sigue muda en
    # este mismo estado. Si algún día `check_and_alert` aprende a ver esto, este
    # assert se cae y hay que reconsiderar si `alert_event` sigue haciendo falta.
    alerter._send.reset_mock()
    await alerter.check_and_alert()
    alerter._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_el_cooldown_no_deja_que_una_caida_spamee_telegram():
    alerter = TelegramAlerter("token", "chat", cooldown_seconds=300)
    alerter._send = AsyncMock()

    for _ in range(5):
        with pytest.raises(RuntimeError):
            await acquire_or_alert(_BrokenPool(), _request_with_alerter(alerter), "search")

    assert alerter._send.await_count == 1
    # Pero los contadores sí suben las 5 veces: el cooldown silencia el aviso, no
    # la evidencia. Es la diferencia entre no spamear y no registrar.
    assert api_metrics.snapshot()["total_pool_failures"] == 5


@pytest.mark.asyncio
async def test_el_cooldown_es_por_evento_y_no_global():
    alerter = TelegramAlerter("token", "chat", cooldown_seconds=300)
    alerter._send = AsyncMock()

    assert await alerter.alert_event("pool_unavailable", "x") is True
    assert await alerter.alert_event("pool_unavailable", "x") is False
    # Un evento distinto no queda tapado por el cooldown del anterior: si lo
    # estuviera, la primera falla del día silenciaría a todas las demás.
    assert await alerter.alert_event("mint_failed", "y") is True


@pytest.mark.asyncio
async def test_un_alerter_roto_no_se_lleva_puesta_la_excepcion_original():
    # El call site hace `raise` DESPUÉS de avisar. Si el aviso lanzara, la app
    # recibiría el fallo del alerter en vez del error real y clasificaría mal la
    # falla: es la única forma de que este arreglo empeore lo que vino a mejorar.
    alerter = TelegramAlerter("token", "chat")
    alerter.alert_event = AsyncMock(side_effect=RuntimeError("telegram caido"))

    with pytest.raises(RuntimeError, match="Connection refused"):
        await acquire_or_alert(_BrokenPool(), _request_with_alerter(alerter), "search")


@pytest.mark.asyncio
async def test_sin_alerter_configurado_igual_cuenta_y_relanza():
    request = MagicMock()
    del request.app.state.alerter

    with pytest.raises(RuntimeError):
        await acquire_or_alert(_BrokenPool(), request, "detail")

    assert api_metrics.snapshot()["total_pool_failures"] == 1
