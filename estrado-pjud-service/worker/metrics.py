# worker/metrics.py
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from worker.config import WorkerConfig

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("America/Santiago")


class Metrics:
    def __init__(self, config: WorkerConfig, supabase):
        self._config = config
        self._sb = supabase
        self.cases_synced_total: int = 0
        self.cases_synced_today: int = 0
        self.errors_today: int = 0
        self._current_day: int = datetime.now(_TZ).day
        self._task: asyncio.Task | None = None

    def record_sync(self):
        self._maybe_reset_daily()
        self.cases_synced_total += 1
        self.cases_synced_today += 1

    def record_error(self):
        self._maybe_reset_daily()
        self.errors_today += 1

    def _maybe_reset_daily(self):
        today = datetime.now(_TZ).day
        if today != self._current_day:
            self.cases_synced_today = 0
            self.errors_today = 0
            self._current_day = today

    async def send_heartbeat(self):
        self._maybe_reset_daily()
        now = datetime.now(_TZ).isoformat()
        self._sb.from_("sync_worker_heartbeats").upsert(
            {
                "worker_id": self._config.WORKER_ID,
                "status": "running",
                "last_heartbeat_at": now,
                "cases_synced_total": self.cases_synced_total,
                "cases_synced_today": self.cases_synced_today,
                "errors_today": self.errors_today,
                "pool_size": self._config.POOL_SIZE,
            },
            on_conflict="worker_id",
        ).execute()
        logger.debug("Heartbeat sent")

    async def _heartbeat_loop(self):
        while True:
            try:
                await self.send_heartbeat()
            except Exception:
                logger.exception("Heartbeat failed")
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
        try:
            now = datetime.now(_TZ).isoformat()
            self._sb.from_("sync_worker_heartbeats").upsert(
                {
                    "worker_id": self._config.WORKER_ID,
                    "status": "stopped",
                    "last_heartbeat_at": now,
                    "cases_synced_total": self.cases_synced_total,
                    "cases_synced_today": self.cases_synced_today,
                    "errors_today": self.errors_today,
                    "pool_size": self._config.POOL_SIZE,
                },
                on_conflict="worker_id",
            ).execute()
        except Exception:
            logger.exception("Final heartbeat failed")
