# worker/metrics.py
import asyncio
import logging
from datetime import datetime, timezone

from worker.config import WorkerConfig, TZ_SANTIAGO, run_query
from worker.sd_notify import notify_watchdog
from worker.maintenance_heartbeat import maintenance_proof

logger = logging.getLogger(__name__)


class Metrics:
    def __init__(self, config: WorkerConfig, supabase, pool=None, proxy_control=None, maintenance=None):
        self._config = config
        self._sb = supabase
        # El pool es opcional para no romper a quien construya Metrics sin el,
        # pero sin el no hay ni tamaño real del pool ni metricas de minteo.
        self._pool = pool
        self._proxy_control = proxy_control
        self._maintenance = maintenance
        self.initialization_started = False
        self.current_status = "starting"
        self.cases_synced_total: int = 0
        self.cases_synced_today: int = 0
        # `errors_today` mezclaba dos cosas distintas: que se caiga el proxy no
        # dice nada de la causa, y una causa que no parsea no dice nada del proxy.
        self.errors_infra_today: int = 0
        self.errors_case_today: int = 0
        self._current_date = datetime.now(TZ_SANTIAGO).date()
        self._task: asyncio.Task | None = None

    @property
    def errors_today(self) -> int:
        """Total. La columna existente no cambia de significado."""
        return self.errors_infra_today + self.errors_case_today

    def record_sync(self):
        self._maybe_reset_daily()
        self.cases_synced_total += 1
        self.cases_synced_today += 1

    def record_error(self, kind: str = "case"):
        """`kind="infra"` para lo que es culpa del proxy/la sesion (bloqueos,
        timeouts, drift de parser) y `"case"` para lo que es de la causa."""
        self._maybe_reset_daily()
        if kind == "infra":
            self.errors_infra_today += 1
        else:
            self.errors_case_today += 1

    def _maybe_reset_daily(self):
        today = datetime.now(TZ_SANTIAGO).date()
        if today != self._current_date:
            self.cases_synced_today = 0
            self.errors_infra_today = 0
            self.errors_case_today = 0
            self._current_date = today

    def set_status(self, status: str) -> None:
        if status not in {
            "starting", "paused", "running", "backoff", "idle_off_hours", "stopped",
        }:
            raise ValueError(f"invalid worker status: {status}")
        self.current_status = status

    def heartbeat_payload(self, status: str | None = None) -> dict:
        """Fila del heartbeat. Una sola fuente para el latido y para el apagado —
        estaban duplicadas y era cuestion de tiempo que divergieran."""
        self._maybe_reset_daily()
        current_status = status or self.current_status
        proof = maintenance_proof(self._maintenance, initialization_started=self.initialization_started)
        if current_status == "stopped":
            proof = None
        attempts = getattr(self._pool, "mint_attempts", 0) if self._pool else 0
        failures = getattr(self._pool, "mint_failures", 0) if self._pool else 0
        raw_slot_counts = (
            getattr(self._pool, "slot_state_counts", {}) if self._pool else {}
        )

        def aggregate_count(name: str, source=raw_slot_counts) -> int:
            value = source.get(name, 0) if isinstance(source, dict) else 0
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        slot_state_counts = {
            "healthy": aggregate_count("healthy"),
            "validate_before_reuse": aggregate_count("validate_before_reuse"),
            "replace_before_reuse": aggregate_count("replace_before_reuse"),
            "cooldown": aggregate_count("cooldown"),
        }
        control = self._proxy_control.snapshot if self._proxy_control else None
        reuse_enabled = (
            getattr(self._config, "WORKER_SESSION_REUSE_VALIDATION_ENABLED", False)
            is True
        )
        rollout_started_at = getattr(
            self._config, "SESSION_REUSE_ROLLOUT_STARTED_AT", None
        )
        rollout_started_at_iso = (
            rollout_started_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            if reuse_enabled and isinstance(rollout_started_at, datetime)
            else None
        )
        return {
            "worker_id": self._config.WORKER_ID,
            "status": "paused" if proof is not None else current_status,
            "last_heartbeat_at": datetime.now(TZ_SANTIAGO).isoformat(),
            "cases_synced_total": self.cases_synced_total,
            "cases_synced_today": self.cases_synced_today,
            "errors_today": self.errors_today,
            # En modo proxy el pool corre con OJV_PROXY_POOL_SIZE slots, no con
            # POOL_SIZE: el heartbeat decia 1 mientras andaban 3.
            "pool_size": (
                self._pool.effective_pool_size if self._pool else self._config.POOL_SIZE
            ),
            "metadata": {
                "maintenance": proof,
                "mint_attempts": attempts,
                "mint_failures": failures,
                "mint_failure_rate": round(failures / attempts, 4) if attempts else 0.0,
                "errors_infra_today": self.errors_infra_today,
                "errors_case_today": self.errors_case_today,
                "proxy_control_status": control.status if control else "unavailable",
                "proxy_control_reason": control.reason_code if control else "not_configured",
                "proxy_control_revision": control.revision if control else None,
                "proxy_control_source": control.source if control else "local",
                "document_inline_enabled": False,
                "session_reuse_validation_enabled": reuse_enabled,
                "session_reuse_canary_stage": (
                    "worker_canary" if reuse_enabled else "off"
                ),
                "session_reuse_rollout_started_at": rollout_started_at_iso,
                "transport_revalidation_enabled": (
                    getattr(
                        self._config,
                        "WORKER_TRANSPORT_REVALIDATION_ENABLED",
                        False,
                    )
                    is True
                ),
                "slot_state_counts": slot_state_counts,
                "session_validation_successes": aggregate_count(
                    "validation_successes", vars(self._pool) if self._pool else {},
                ),
                "session_validation_failures": aggregate_count(
                    "validation_failures", vars(self._pool) if self._pool else {},
                ),
                "session_validations_avoided_mint": aggregate_count(
                    "validations_avoided_mint", vars(self._pool) if self._pool else {},
                ),
                "process_outside_office_hours_enabled": (
                    self._config.PJUD_PROCESS_OUTSIDE_OFFICE_HOURS is True
                ),
            },
        }

    async def send_heartbeat(self) -> bool:
        """Publica telemetria sin convertir una falla externa en una caida.

        El heartbeat es best-effort: systemd ya reinicia el proceso si la señal
        permanece ausente, pero un 400/5xx transitorio de Supabase no debe
        interrumpir el trabajo judicial ni escapar al loop principal.
        """
        try:
            await run_query(
                self._sb.from_("sync_worker_heartbeats").upsert(
                    self.heartbeat_payload(),
                    on_conflict="worker_id",
                )
            )
        except Exception:
            logger.exception("Heartbeat failed")
            return False

        notify_watchdog()
        logger.debug("Heartbeat sent")
        return True

    async def _heartbeat_loop(self):
        while True:
            await self.send_heartbeat()
            await asyncio.sleep(self._config.HEARTBEAT_INTERVAL_S)

    def start(self):
        self._task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final heartbeat with stopped status
        self.set_status("stopped")
        try:
            await run_query(
                self._sb.from_("sync_worker_heartbeats").upsert(
                    self.heartbeat_payload(),
                    on_conflict="worker_id",
                )
            )
        except Exception:
            logger.exception("Final heartbeat failed")
