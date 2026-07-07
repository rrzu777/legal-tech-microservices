# worker/__main__.py
import asyncio
import logging
import signal
import sys
import json
import time

from app.alerting import send_ops_alert
from app.bandwidth import METER
from worker.config import WorkerConfig
from worker.supabase_client import create_supabase
from worker.session_pool import SessionPool
from worker.scheduler import Scheduler
from worker.engine import SyncEngine
from worker.notifier import Notifier
from worker.metrics import Metrics
from worker.backoff import CircuitBreaker
from worker.sd_notify import notify_ready, notify_watchdog, notify_stopping

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


def setup_logging(level: str):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.root.handlers = [handler]
    logging.root.setLevel(getattr(logging, level.upper(), logging.INFO))


async def process_batch(batch, engine, concurrency, shutdown_event, backoff):
    """Process a batch of cases concurrently, bounded to `concurrency` in-flight
    at a time (matches the number of residential IP slots in the pool).

    Cases already dispatched (past the semaphore gate) are allowed to finish
    even if shutdown is requested or the circuit breaker opens mid-batch; only
    not-yet-started cases are skipped, for a graceful drain.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _run_one(case):
        async with sem:
            if shutdown_event.is_set() or backoff.is_open:
                return
            try:
                await engine.sync_case(case)
            except Exception:
                logger.exception("Unhandled error syncing case %s", case.get("id"))

    results = await asyncio.gather(*(_run_one(c) for c in batch), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.exception("Unhandled exception in process_batch task", exc_info=result)


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


async def safe_initialize_pool(pool, max_retries: int = 5, base_delay: int = 10) -> bool:
    """Inicializa el pool con backoff; devuelve False si falla tras los reintentos,
    sin crashear (evita el crash-loop de systemd martillando PJUD)."""
    for attempt in range(1, max_retries + 1):
        try:
            await pool.initialize()
            return True
        except Exception:
            logger.exception("Fallo al inicializar el pool (intento %d/%d)", attempt, max_retries)
            if attempt < max_retries:
                await asyncio.sleep(base_delay * attempt)
    return False


async def main():
    config = WorkerConfig()
    setup_logging(config.LOG_LEVEL)
    logger.info("Starting worker %s (pool_size=%d)", config.WORKER_ID, config.POOL_SIZE)

    supabase = create_supabase(config)
    pool = SessionPool(config)
    scheduler = Scheduler(config, supabase)
    notifier = Notifier(supabase)
    metrics = Metrics(config, supabase)
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

    # Initialize session pool
    if not await safe_initialize_pool(pool, max_retries=config.MINT_MAX_RETRIES):
        logger.error(
            "No se pudo inicializar el pool tras %d reintentos; worker queda inactivo pero vivo",
            config.MINT_MAX_RETRIES,
        )
        await send_ops_alert(
            config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID,
            "mint_failed", f"Worker {config.WORKER_ID}: no se pudo inicializar el pool (minteo).",
        )
        await shutdown_event.wait()
        return

    metrics.start()

    engine = SyncEngine(
        pool=pool,
        supabase=supabase,
        notifier=notifier,
        metrics=metrics,
        backoff=backoff,
        config=config,
    )

    logger.info("Worker ready, entering main loop")
    notify_ready()

    bandwidth_alert_state = BandwidthAlertState()

    try:
        while not shutdown_event.is_set():
            if backoff.is_open:
                wait = backoff.seconds_until_close
                logger.warning("Circuit breaker open, waiting %.0fs", wait)
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=min(wait, 30))
                except asyncio.TimeoutError:
                    pass
                continue

            batch = await scheduler.get_next_batch()

            if not batch:
                logger.debug("No cases to sync, sleeping 30s")
                await metrics.send_heartbeat()
                await maybe_alert_bandwidth(config, bandwidth_alert_state)
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue

            case_ids = [c["id"] for c in batch]

            concurrency = config.OJV_PROXY_POOL_SIZE if config.OJV_PROXY_URL else config.POOL_SIZE
            await process_batch(batch, engine, concurrency, shutdown_event, backoff)

            await scheduler.release_batch(case_ids)
            await metrics.send_heartbeat()
            await maybe_alert_bandwidth(config, bandwidth_alert_state)

    finally:
        notify_stopping()
        logger.info("Shutting down...")
        await metrics.stop()
        await pool.close_all()
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
