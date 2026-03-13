# worker/__main__.py
import asyncio
import logging
import signal
import sys
import json

from worker.config import WorkerConfig
from worker.supabase_client import create_supabase
from worker.session_pool import SessionPool
from worker.scheduler import Scheduler
from worker.engine import SyncEngine
from worker.notifier import Notifier
from worker.metrics import Metrics
from worker.backoff import CircuitBreaker

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
        block_pause_seconds=3600,  # 60 min on OJV block
    )

    shutdown_event = asyncio.Event()

    def handle_signal(sig, _frame):
        logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Initialize session pool
    await pool.initialize()
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
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue

            case_ids = [c["id"] for c in batch]

            for case in batch:
                if shutdown_event.is_set():
                    break
                if backoff.is_open:
                    break
                await engine.sync_case(case)

            await scheduler.release_batch(case_ids)
            await metrics.send_heartbeat()

    finally:
        logger.info("Shutting down...")
        await metrics.stop()
        await pool.close_all()
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
