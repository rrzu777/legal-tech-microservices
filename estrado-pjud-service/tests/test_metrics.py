"""Tests for API metrics collection."""
import pytest

from app.config import Settings


class TestAPIMetrics:
    def test_record_request_increments_counters(self):
        from app.metrics import api_metrics

        api_metrics.record_request("search")
        api_metrics.record_request("search")
        api_metrics.record_request("detail")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_requests"] == 3
        assert snapshot["search_requests"] == 2
        assert snapshot["detail_requests"] == 1

    def test_record_error_increments_counters(self):
        from app.metrics import api_metrics

        api_metrics.record_error("search")
        api_metrics.record_blocked("detail")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_errors"] == 1
        assert snapshot["total_blocked"] == 1

    def test_blocked_rate_calculation(self):
        from app.metrics import api_metrics

        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(3):
            api_metrics.record_blocked("search")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_requests"] == 10
        assert snapshot["total_blocked"] == 3
        assert snapshot["blocked_rate"] == pytest.approx(0.3, abs=0.01)

    def test_snapshot_includes_uptime(self):
        from app.metrics import api_metrics

        snapshot = api_metrics.snapshot()
        assert "uptime_seconds" in snapshot
        assert isinstance(snapshot["uptime_seconds"], int)

    def test_no_requests_blocked_rate_is_zero(self):
        from app.metrics import api_metrics

        snapshot = api_metrics.snapshot()
        assert snapshot["blocked_rate"] == 0.0

    def test_windowed_blocked_rate(self):
        from app.metrics import api_metrics

        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(3):
            api_metrics.record_blocked("search")

        rate = api_metrics.windowed_blocked_rate()
        assert rate == pytest.approx(0.3, abs=0.01)  # 3 blocked / 10 requests

    def test_windowed_rate_zero_when_empty(self):
        from app.metrics import api_metrics
        assert api_metrics.windowed_blocked_rate() == 0.0


class TestWorkerMetrics:
    """B2 — el heartbeat mentia: reportaba pool_size=1 con 3 slots corriendo y
    mezclaba errores de infra con errores de causa en un solo numero."""

    def _make(self, pool=None):
        from unittest.mock import MagicMock
        from worker.metrics import Metrics
        config = MagicMock()
        config.WORKER_ID = "vps-worker-1"
        config.POOL_SIZE = 1
        return Metrics(config, MagicMock(), pool=pool)

    def _fake_pool(self, size=3, attempts=0, failures=0):
        from unittest.mock import MagicMock
        pool = MagicMock()
        pool.effective_pool_size = size
        pool.mint_attempts = attempts
        pool.mint_failures = failures
        return pool

    def test_pool_size_reporta_el_tamano_efectivo(self):
        """config.POOL_SIZE es 1, pero en modo proxy el pool corre con
        OJV_PROXY_POOL_SIZE (3) slots. El heartbeat en produccion decia 1."""
        m = self._make(pool=self._fake_pool(size=3))
        assert m.heartbeat_payload("running")["pool_size"] == 3

    def test_sin_pool_cae_al_config(self):
        m = self._make(pool=None)
        assert m.heartbeat_payload("running")["pool_size"] == 1

    def test_separa_errores_de_infra_y_de_causa(self):
        m = self._make(pool=self._fake_pool())
        m.record_error("infra")
        m.record_error("infra")
        m.record_error("case")

        meta = m.heartbeat_payload("running")["metadata"]
        assert meta["errors_infra_today"] == 2
        assert meta["errors_case_today"] == 1

    def test_errors_today_sigue_siendo_el_total(self):
        """La columna existente no cambia de significado: quien la lea hoy
        sigue viendo el total."""
        m = self._make(pool=self._fake_pool())
        m.record_error("infra")
        m.record_error("case")
        assert m.heartbeat_payload("running")["errors_today"] == 2

    def test_default_es_error_de_causa(self):
        m = self._make(pool=self._fake_pool())
        m.record_error()
        assert m.heartbeat_payload("running")["metadata"]["errors_case_today"] == 1

    def test_metadata_lleva_las_metricas_de_minteo(self):
        m = self._make(pool=self._fake_pool(attempts=50, failures=6))
        meta = m.heartbeat_payload("running")["metadata"]
        assert meta["mint_attempts"] == 50
        assert meta["mint_failures"] == 6
        assert meta["mint_failure_rate"] == 0.12

    def test_tasa_de_minteo_sin_intentos_no_divide_por_cero(self):
        m = self._make(pool=self._fake_pool(attempts=0, failures=0))
        assert m.heartbeat_payload("running")["metadata"]["mint_failure_rate"] == 0.0

    def test_el_reset_diario_limpia_los_dos_contadores(self):
        from datetime import date
        m = self._make(pool=self._fake_pool())
        m.record_error("infra")
        m.record_error("case")
        m._current_date = date(2000, 1, 1)  # fuerza el cambio de dia

        meta = m.heartbeat_payload("running")["metadata"]
        assert meta["errors_infra_today"] == 0
        assert meta["errors_case_today"] == 0


#: El mismo umbral que usa el alerter de Telegram, leído de la config real y no
#: escrito a mano: el punto del parámetro es que el panel y la alerta no puedan
#: tener dos números distintos, y un literal acá dejaría de verificar eso.
_THRESHOLD = Settings.model_fields["TELEGRAM_BLOCKED_RATE_THRESHOLD"].default


class TestStatusDerivado:
    """El `status` de /api/v1/health, que estaba hardcodeado en "ok".

    El 31 de julio de 2026 esa constante decía `ok` mientras la instancia
    llevaba 3 días y 18 horas devolviendo 500 a todas las consultas. Un watchdog
    externo mira `.status` antes que cualquier contador.
    """

    def test_sin_trafico_reciente_no_esta_roto(self):
        from app.metrics import api_metrics

        # Ocioso no es lo mismo que caído, y esta función no puede distinguir
        # "nadie me llamó" de "el cron que me llamaba está muerto". Detectar el
        # SILENCIO es del watchdog externo, que sabe cuánto tráfico debería haber.
        assert api_metrics.status(_THRESHOLD) == "ok"

    def test_el_pool_sin_sesion_y_cero_exitos_es_down(self):
        from app.metrics import api_metrics

        # El estado del 31 de julio, y el único que una detección por PROPORCIÓN
        # no puede ver: el fallo ocurre ANTES de `record_request`, así que el
        # denominador también queda en cero y el alerter sale temprano.
        api_metrics.record_pool_failure("search")
        api_metrics.record_pool_failure("detail")

        assert api_metrics.status(_THRESHOLD) == "down"

    def test_pedidos_sin_un_solo_exito_es_down(self):
        from app.metrics import api_metrics

        for _ in range(3):
            api_metrics.record_request("search")
            api_metrics.record_error("search")

        assert api_metrics.status(_THRESHOLD) == "down"

    def test_un_exito_reciente_lo_saca_de_down(self):
        from app.metrics import api_metrics

        api_metrics.record_pool_failure("search")
        api_metrics.record_request("search")
        api_metrics.record_success("search")

        # La ventana se limpia sola: por eso el estado se deriva de ella y no de
        # `total_pool_failures`, que es monótono y dejaría "down" para siempre.
        assert api_metrics.status(_THRESHOLD) == "ok"

    def test_la_mitad_bloqueada_es_degraded(self):
        from app.metrics import api_metrics

        for _ in range(4):
            api_metrics.record_request("search")
        api_metrics.record_success("search")
        api_metrics.record_blocked("search")
        api_metrics.record_blocked("search")

        assert api_metrics.status(_THRESHOLD) == "degraded"

    def test_bloqueos_sueltos_no_son_degraded(self):
        from app.metrics import api_metrics

        for _ in range(10):
            api_metrics.record_request("search")
        api_metrics.record_success("search")
        api_metrics.record_blocked("search")

        assert api_metrics.status(_THRESHOLD) == "ok"


class TestFamiliaEnElSnapshot:
    def test_familia_tiene_su_propio_contador(self):
        """La ruta de Familia no tenía UNA sola llamada a `api_metrics`, así que
        sus consultas no aparecían en ningún contador — y es la ruta que usan las
        causas con credencial del abogado."""
        from app.metrics import api_metrics

        api_metrics.record_request("familia")
        api_metrics.record_request("familia")

        snapshot = api_metrics.snapshot()
        assert snapshot["familia_requests"] == 2
        assert snapshot["total_requests"] == 2

    def test_familia_requests_llega_a_la_respuesta_de_health(self):
        """Guarda contra el drop silencioso: `HealthResponse` se construye con
        `**snapshot` y pydantic IGNORA las claves que el modelo no declara, así
        que una métrica nueva puede quedar contada y nunca publicada."""
        from app.models import HealthResponse

        assert "familia_requests" in HealthResponse.model_fields
