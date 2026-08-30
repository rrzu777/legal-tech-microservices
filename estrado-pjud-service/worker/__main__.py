# worker/__main__.py
import asyncio
import logging
import signal
import sys
import json
import time

from app.alerting import send_ops_alert
from app.bandwidth import METER
from app.proxy_billing import is_proxy_billing_error
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.logging_redaction import install_secret_redaction
from worker.config import WorkerConfig
from worker.supabase_client import create_supabase
from worker.session_pool import SessionPool
from worker.scheduler import (
    Scheduler,
    is_processing_allowed,
    is_scheduled_processing_window,
)
from worker.engine import SyncEngine
from worker.notifier import Notifier
from worker.metrics import Metrics
from worker.backoff import CircuitBreaker
from worker.sd_notify import notify_ready, notify_status, notify_stopping
from worker.proxy_control import ProxyControl, ProxyControlSnapshot
from worker.proxy_usage import ProxyUsageTracker
from worker.maintenance import WorkerMaintenance, has_active_operation, mark_uncertain, track_auxiliary
from worker.maintenance_store import MaintenanceError, MaintenanceStore, ProcessIdentity

logger = logging.getLogger("worker")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging(level: str, *, secrets: tuple[str, ...] = ()):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    install_secret_redaction((handler,), secrets)
    logging.root.handlers = [handler]
    logging.root.setLevel(getattr(logging, level.upper(), logging.INFO))


async def process_batch(
    batch: list,
    engine: SyncEngine,
    concurrency: int,
    shutdown_event: asyncio.Event,
    backoff: CircuitBreaker,
    proxy_control: ProxyControl | None = None,
    processing_window=is_scheduled_processing_window,
    shutdown_grace_seconds: float = 30.0,
) -> None:
    """Process a batch of cases concurrently, bounded to `concurrency` in-flight
    at a time (matches the number of residential IP slots in the pool).

    Cases already dispatched get a bounded graceful drain after shutdown;
    unfinished work is then cancelled and remains unacknowledged for lease
    takeover. Not-yet-started cases are skipped immediately.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _run_one(case):
        async with sem:
            # Un lote reclamado al final de la jornada puede seguir esperando
            # el semáforo después de las 18:00. No iniciar tráfico nuevo cuando
            # finalmente obtiene un slot; next_sync_at queda vencido para la
            # próxima apertura.
            if not processing_window():
                return
            if proxy_control is not None:
                snapshot = await refresh_proxy_gate(proxy_control, backoff)
                if not snapshot.allowed:
                    return
            if shutdown_event.is_set() or backoff.is_open:
                return
            try:
                await engine.sync_case(case)
            except Exception:
                if has_active_operation():
                    mark_uncertain()
                logger.exception("Unhandled error syncing case %s", case.get("id"))

    if (
        isinstance(shutdown_grace_seconds, bool)
        or not isinstance(shutdown_grace_seconds, (int, float))
        or shutdown_grace_seconds < 0
    ):
        raise ValueError("invalid_shutdown_grace_seconds")

    admitted = has_active_operation()
    case_tasks = []
    for case in batch:
        task = asyncio.create_task(_run_one(case))
        if admitted:
            track_auxiliary(task)
        case_tasks.append(task)
    batch_future = asyncio.gather(*case_tasks, return_exceptions=True)
    shutdown_wait = asyncio.create_task(shutdown_event.wait())
    try:
        done, _ = await asyncio.wait(
            {batch_future, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED,
        )
        if batch_future in done:
            await batch_future
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(batch_future), timeout=float(shutdown_grace_seconds),
            )
        except asyncio.TimeoutError:
            for task in case_tasks:
                if not task.done():
                    task.cancel()
            await batch_future
    finally:
        # External parent cancellation must not orphan a case task or let the
        # caller release the batch before its case cleanup has joined. A hold
        # never sets shutdown_event or cancels any of these tasks.
        for task in case_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*case_tasks, return_exceptions=True)
        if not shutdown_wait.done():
            shutdown_wait.cancel()
        await asyncio.gather(shutdown_wait, return_exceptions=True)


BANDWIDTH_ALERT_COOLDOWN_S = 6 * 60 * 60  # 6h, avoid spamming ops once over budget


class BandwidthAlertState:
    """Tracks when the last bandwidth alert fired, so we only alert once per
    cooldown window instead of on every loop iteration."""

    def __init__(self):
        self.last_alert_ts: float | None = None


async def maybe_alert_bandwidth(config, state: "BandwidthAlertState", now: float | None = None) -> None:
    """Fire-and-forget check: alert ops when proxy bandwidth usage crosses
    OJV_PROXY_GB_ALERT_PCT% of OJV_PROXY_GB_BUDGET. No-op in no-proxy mode
    (OJV_PROXY_URL unset) and rate-limited to once per cooldown window.
    """
    if not config.OJV_PROXY_URL:
        return

    threshold_gb = config.OJV_PROXY_GB_BUDGET * config.OJV_PROXY_GB_ALERT_PCT / 100
    if METER.total_gb < threshold_gb:
        return

    now = time.monotonic() if now is None else now
    if state.last_alert_ts is not None and (now - state.last_alert_ts) < BANDWIDTH_ALERT_COOLDOWN_S:
        return

    state.last_alert_ts = now
    await send_ops_alert(
        config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID,
        "bandwidth_high",
        f"Proxy bandwidth {METER.total_gb:.2f}GB / budget {config.OJV_PROXY_GB_BUDGET}GB "
        f"(>{config.OJV_PROXY_GB_ALERT_PCT}%)",
    )


async def safe_initialize_pool(
    pool, max_retries: int = 5, base_delay: int = 10,
    proxy_control=None, backoff=None,
) -> bool:
    """Inicializa el pool con backoff; devuelve False si falla tras los reintentos,
    sin crashear (evita el crash-loop de systemd martillando PJUD)."""
    for attempt in range(1, max_retries + 1):
        try:
            await pool.initialize()
            return True
        except Exception as exc:
            logger.exception("Fallo al inicializar el pool (intento %d/%d)", attempt, max_retries)
            if is_proxy_billing_error(exc):
                if proxy_control is not None:
                    await proxy_control.trip_billing_exhausted()
                if backoff is not None:
                    backoff.open_permanently("billing_exhausted")
                return False
            if isinstance(exc, (ProxyBudgetExceededError, ProxyUsagePersistenceError)):
                if proxy_control is not None:
                    if isinstance(exc, ProxyUsagePersistenceError):
                        await proxy_control.pause_telemetry_unavailable()
                    else:
                        await proxy_control.refresh()
                if backoff is not None:
                    backoff.open_permanently("proxy_cost_control")
                return False
            if attempt < max_retries:
                await asyncio.sleep(base_delay * attempt)
    return False


def can_initialize_paid_pool(
    now=None,
    *,
    validation_once: bool = False,
    process_outside_office_hours: bool = False,
) -> bool:
    """El pool residencial solo puede mintear cuando el scheduler puede trabajar."""
    return is_processing_allowed(
        now,
        validation_once=validation_once,
        process_outside_office_hours=process_outside_office_hours,
    )


async def wait_before_retry(
    shutdown_event: asyncio.Event,
    timeout: float,
    *,
    validation_once: bool,
) -> bool:
    """Return False instead of leaving a transient validation unit waiting."""
    if validation_once:
        return False
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return True


async def safe_get_next_batch(scheduler, metrics, backoff):
    """Un fallo del claim no debe cerrar el proceso y provocar otro minteo.

    `None` distingue infraestructura caída de un lote legítimamente vacío.
    """
    try:
        return await scheduler.get_next_batch()
    except Exception:
        if has_active_operation():
            mark_uncertain()
        logger.exception("Failed to claim PJUD sync batch; keeping pool alive")
        metrics.record_error("infra")
        backoff.record_failure()
        return None


async def safe_process_import_job(engine, metrics) -> bool:
    """Poll one discovery job without coupling failures to public case sync."""
    try:
        return await engine.process_import_job()
    except Exception as exc:
        if has_active_operation():
            mark_uncertain()
        logger.error(
            "Failed to process PJUD import discovery job (error_class=%s)",
            type(exc).__name__,
        )
        metrics.record_error("infra")
        if is_proxy_billing_error(exc):
            proxy_control = getattr(engine, "_proxy_control", None)
            if proxy_control is not None:
                await proxy_control.trip_billing_exhausted()
            backoff = getattr(engine, "_backoff", None)
            if backoff is not None:
                backoff.open_permanently("billing_exhausted")
        return False


async def run_import_discovery_loop(
    engine,
    metrics,
    shutdown_event: asyncio.Event,
    *,
    poll_interval: float = 5.0,
    traffic_allowed=lambda: True,
    maintenance: WorkerMaintenance | None = None,
) -> None:
    """Independent one-at-a-time discovery loop with structured cancellation."""
    while not shutdown_event.is_set():
        if traffic_allowed():
            try:
                processed = (
                    await maintenance.run(lambda: safe_process_import_job(engine, metrics))
                    if maintenance is not None else await safe_process_import_job(engine, metrics)
                )
            except MaintenanceError:
                processed = False
            if processed:
                continue
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass


def public_sync_concurrency(session_capacity: int, *, imports_enabled: bool) -> int:
    """Reserve one pool session for imports without ever starving public sync."""
    return max(1, session_capacity - (1 if imports_enabled else 0))


async def safe_reconcile_stale_runs(scheduler, metrics, backoff) -> bool:
    """Run bounded maintenance fail-closed before any traffic gate or claim."""
    try:
        await scheduler.reconcile_stale_runs()
        return True
    except Exception:
        if has_active_operation():
            mark_uncertain()
        logger.exception("Failed to reconcile stale PJUD sync runs; traffic blocked")
        metrics.record_error("infra")
        backoff.record_failure()
        return False


async def scheduler_contract_ready(scheduler, metrics, backoff) -> bool:
    """Falla cerrado antes del primer mint si la migración/RPC no está lista."""
    try:
        await scheduler.verify_claim_contract()
        return True
    except Exception:
        logger.exception("PJUD scheduler contract unavailable; refusing to mint")
        metrics.record_error("infra")
        backoff.record_failure()
        return False


async def refresh_proxy_gate(
    control: ProxyControl | None, backoff: CircuitBreaker,
) -> ProxyControlSnapshot:
    """Synchronize the local breaker with the persistent ops control."""
    if control is None:
        return ProxyControlSnapshot(
            allowed=True,
            status="not_required",
            reason_code=None,
            revision=None,
            source="local",
        )
    snapshot = await control.refresh()
    if snapshot.allowed:
        backoff.resume_permanent()
    else:
        backoff.open_permanently(f"proxy_control:{snapshot.status}")
    return snapshot


async def watch_maintenance(
    maintenance: WorkerMaintenance, shutdown_event: asyncio.Event, *, poll_interval: float = 1.0,
) -> None:
    """Independent ACK publisher, including while the main loop is gated."""
    try:
        while not shutdown_event.is_set():
            try:
                maintenance.publish_ack()
            except Exception:
                maintenance.mark_uncertain()
                logger.error("Maintenance acknowledgement unavailable; admission closed")
            try:
                async with asyncio.timeout(poll_interval):
                    await shutdown_event.wait()
            except TimeoutError:
                pass
    except BaseException:
        if not shutdown_event.is_set():
            maintenance.mark_uncertain()
        raise


async def initialize_worker_attempt(
    pool, scheduler, metrics, backoff, proxy_control, config, *,
    validation_once: bool, process_outside_office_hours: bool, imports_enabled: bool,
) -> str:
    """One bounded startup operation; idle/retry waits belong outside admission."""
    if not await safe_reconcile_stale_runs(scheduler, metrics, backoff):
        return "reconcile_unavailable"
    snapshot = await refresh_proxy_gate(proxy_control, backoff)
    if not snapshot.allowed:
        return "paused"
    prewarm = can_initialize_paid_pool(
        validation_once=validation_once,
        process_outside_office_hours=process_outside_office_hours,
    )
    if not prewarm and not imports_enabled:
        return "idle_off_hours"
    if not await scheduler_contract_ready(scheduler, metrics, backoff):
        return "contract_unavailable"
    if not prewarm:
        # Manual imports still claim/authorize before lazy paid checkout.
        await pool.initialize(prewarm=False)
        return "ready"
    metrics.set_status("starting")
    notify_status("initializing proxy pool")
    initialized = await safe_initialize_pool(
        pool, max_retries=1 if validation_once else config.MINT_MAX_RETRIES,
        proxy_control=proxy_control, backoff=backoff,
    )
    return "ready" if initialized else "mint_failed"


async def reconcile_and_refresh(scheduler, metrics, backoff, proxy_control):
    if not await safe_reconcile_stale_runs(scheduler, metrics, backoff):
        return None
    return await refresh_proxy_gate(proxy_control, backoff)


async def claim_process_release_batch(
    scheduler, metrics, backoff, engine, concurrency, shutdown_event, *,
    proxy_control=None, processing_window=is_scheduled_processing_window,
):
    """The caller admits before claim and retains ownership through release."""
    batch = await safe_get_next_batch(scheduler, metrics, backoff)
    if not batch:
        return batch
    await process_batch(
        batch, engine, concurrency, shutdown_event, backoff,
        proxy_control=proxy_control, processing_window=processing_window,
    )
    await scheduler.release_batch([case["id"] for case in batch])
    return batch


async def main():
    config = WorkerConfig()
    validation_once = config.PJUD_OFF_HOURS_VALIDATION_ONCE is True
    process_outside_office_hours = (
        config.PJUD_PROCESS_OUTSIDE_OFFICE_HOURS is True
    )
    if validation_once:
        # Una causa no necesita un pool de tres salidas. Esta mutación sólo vive
        # en el proceso transitorio y acota el tráfico de adquisición a un slot
        # y una IP. Si falla, el operador diagnostica sin rotar en loop.
        config.OJV_PROXY_POOL_SIZE = 1
        config.POOL_SIZE = 1
        config.MINT_MAX_RETRIES = 1
    session_capacity = (
        config.OJV_PROXY_POOL_SIZE if config.OJV_PROXY_URL else config.POOL_SIZE
    )
    imports_enabled = (
        config.ENABLE_PJUD_MY_CAUSES_IMPORT is True
        and session_capacity >= 2
        and not validation_once
    )
    setup_logging(
        config.LOG_LEVEL,
        secrets=tuple(filter(None, (
            config.TELEGRAM_BOT_TOKEN,
            config.SUPABASE_SERVICE_KEY,
            config.OJV_PROXY_URL,
        ))),
    )
    logger.info("Starting worker %s (pool_size=%d)", config.WORKER_ID, config.POOL_SIZE)

    # Mandatory local protocol; no environment switch or legacy-open fallback.
    maintenance = WorkerMaintenance(MaintenanceStore.production(), ProcessIdentity.current())

    supabase = create_supabase(config)
    proxy_usage = ProxyUsageTracker(
        supabase,
        enabled=bool(config.OJV_PROXY_URL),
        price_per_gb_usd=config.OJV_PROXY_PRICE_PER_GB_USD,
    )
    proxy_control = ProxyControl(supabase) if config.OJV_PROXY_URL else None
    pool = SessionPool(
        config, proxy_usage=proxy_usage, proxy_control=proxy_control,
    )
    scheduler = Scheduler(config, supabase)
    notifier = Notifier(supabase)
    metrics = Metrics(config, supabase, pool=pool, proxy_control=proxy_control)
    backoff = CircuitBreaker(
        failure_threshold=5,
        pause_seconds=600,      # 10 min on errors
        block_pause_seconds=config.BLOCK_PAUSE_S,  # re-mint recupera el bloqueo; la pausa solo rate-limita el minteo
    )

    shutdown_event = asyncio.Event()

    def handle_signal(sig, _frame):
        logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    metrics.start()
    notify_ready()
    notify_status("starting")
    bandwidth_alert_state = BandwidthAlertState()
    import_task: asyncio.Task | None = None
    maintenance_task = asyncio.create_task(watch_maintenance(maintenance, shutdown_event),
                                          name="pjud-maintenance-ack")

    try:
        initialized = False
        while not shutdown_event.is_set() and not initialized:
            try:
                outcome = await maintenance.run(lambda: initialize_worker_attempt(
                    pool, scheduler, metrics, backoff, proxy_control, config,
                    validation_once=validation_once,
                    process_outside_office_hours=process_outside_office_hours,
                    imports_enabled=imports_enabled,
                ))
            except MaintenanceError:
                metrics.set_status("paused")
                notify_status("maintenance admission closed")
                # A held one-shot must also remain alive and acknowledge hold.
                await wait_before_retry(shutdown_event, 1, validation_once=False)
                continue
            if outcome == "ready":
                initialized = True
                break

            if outcome != "mint_failed":
                status, message = {
                    "reconcile_unavailable": ("backoff", "sync-run reconciliation unavailable; traffic blocked"),
                    "paused": ("paused", "paused"),
                    "idle_off_hours": ("idle_off_hours", "idle outside PJUD office hours"),
                    "contract_unavailable": ("backoff", "scheduler migration unavailable; mint blocked"),
                }[outcome]
                metrics.set_status(status)
                notify_status(message)
                if not await wait_before_retry(shutdown_event, 30, validation_once=validation_once):
                    return
                continue

            logger.error(
                "No se pudo inicializar el pool tras %d reintentos; worker queda inactivo pero vivo",
                config.MINT_MAX_RETRIES,
            )
            await send_ops_alert(
                config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID,
                "mint_failed", f"Worker {config.WORKER_ID}: no se pudo inicializar el pool (minteo).",
            )
            if validation_once:
                logger.error("One-shot validation stopped after mint failure")
                return
            if not backoff.is_permanently_open:
                await shutdown_event.wait()
                return

        if shutdown_event.is_set():
            return

        engine = SyncEngine(
            pool=pool,
            supabase=supabase,
            notifier=notifier,
            metrics=metrics,
            backoff=backoff,
            config=config,
            proxy_control=proxy_control,
            proxy_usage=proxy_usage,
        )
        logger.info("Worker ready, entering main loop")
        metrics.set_status("running")
        notify_status("running")

        if imports_enabled:
            import_task = asyncio.create_task(
                run_import_discovery_loop(
                    engine,
                    metrics,
                    shutdown_event,
                    traffic_allowed=lambda: not backoff.is_open,
                    maintenance=maintenance,
                ),
                name="pjud-import-discovery",
            )
            logger.info(
                "Import discovery loop enabled with one reserved budget; public capacity=%d",
                session_capacity - 1,
            )
        else:
            logger.warning(
                "Import discovery loop disabled (enabled=%s session_capacity=%d validation_once=%s)",
                config.ENABLE_PJUD_MY_CAUSES_IMPORT,
                session_capacity,
                validation_once,
            )

        while not shutdown_event.is_set():
            try:
                snapshot = await maintenance.run(lambda: reconcile_and_refresh(
                    scheduler, metrics, backoff, proxy_control,
                ))
            except MaintenanceError:
                metrics.set_status("paused")
                notify_status("maintenance admission closed")
                await wait_before_retry(shutdown_event, 1, validation_once=False)
                continue
            if snapshot is None:
                metrics.set_status("backoff")
                notify_status("sync-run reconciliation unavailable; traffic blocked")
                if not await wait_before_retry(
                    shutdown_event, 30, validation_once=validation_once,
                ):
                    return
                continue

            if not snapshot.allowed:
                metrics.set_status("paused")
                notify_status("paused")
                logger.warning(
                    "Proxy traffic paused (status=%s reason=%s revision=%s)",
                    snapshot.status, snapshot.reason_code, snapshot.revision,
                )
                if not await wait_before_retry(
                    shutdown_event, 30, validation_once=validation_once,
                ):
                    return
                continue

            if not is_processing_allowed(
                validation_once=validation_once,
                process_outside_office_hours=process_outside_office_hours,
            ):
                metrics.set_status("idle_off_hours")
                notify_status(
                    "scheduled sync outside office hours; manual imports available"
                    if imports_enabled else "idle outside PJUD office hours"
                )
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue

            if backoff.is_open:
                metrics.set_status("backoff")
                notify_status("temporary backoff")
                wait = backoff.seconds_until_close
                logger.warning("Circuit breaker open, waiting %.0fs", wait)
                if not await wait_before_retry(
                    shutdown_event, min(wait, 30), validation_once=validation_once,
                ):
                    return
                continue

            metrics.set_status("running")
            notify_status("running")

            concurrency = public_sync_concurrency(
                session_capacity, imports_enabled=import_task is not None,
            )
            try:
                batch = await maintenance.run(lambda: claim_process_release_batch(
                    scheduler, metrics, backoff, engine, concurrency, shutdown_event,
                    proxy_control=proxy_control,
                    processing_window=lambda: is_processing_allowed(
                        validation_once=validation_once,
                        process_outside_office_hours=process_outside_office_hours,
                    ),
                ))
            except MaintenanceError:
                metrics.set_status("paused")
                notify_status("maintenance admission closed")
                await wait_before_retry(shutdown_event, 1, validation_once=False)
                continue

            if batch is None:
                metrics.set_status("backoff")
                notify_status("scheduler unavailable; pool kept alive")
                if validation_once:
                    logger.error("One-shot validation stopped after scheduler failure")
                    return
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue

            if not batch:
                logger.debug("No cases to sync, sleeping 30s")
                await maybe_alert_bandwidth(config, bandwidth_alert_state)
                if validation_once:
                    logger.info("One-shot validation found no eligible case")
                    return
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue

            await maybe_alert_bandwidth(config, bandwidth_alert_state)
            if validation_once:
                logger.info("One-shot validation completed one batch")
                return

    finally:
        notify_stopping()
        logger.info("Shutting down...")
        shutdown_event.set()
        await asyncio.gather(maintenance_task, return_exceptions=True)
        if 'engine' in locals():
            engine.stop_accepting_work()
        if import_task is not None:
            import_task.cancel()
            await asyncio.gather(import_task, return_exceptions=True)
        if 'engine' in locals():
            await engine.drain_work(timeout_seconds=5.0)
        await metrics.stop()
        await pool.close_all()
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
