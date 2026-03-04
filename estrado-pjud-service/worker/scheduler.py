import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from worker.config import WorkerConfig

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("America/Santiago")


def _is_office_hours(dt: datetime | None = None) -> bool:
    now = dt or datetime.now(_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ)
    return now.weekday() < 5 and 8 <= now.hour < 18


def _is_archived_window(dt: datetime | None = None) -> bool:
    now = dt or datetime.now(_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ)
    return now.weekday() == 6 and (now.hour >= 22 or now.hour < 6)


class Scheduler:
    def __init__(self, config: WorkerConfig, supabase):
        self._config = config
        self._sb = supabase

    async def get_next_batch(self) -> list[dict]:
        now_iso = datetime.now(_TZ).isoformat()

        query = (
            self._sb.from_("cases")
            .select("*")
            .eq("tracking_status", "active")
            .eq("source_system", "pjud_ojv")
            .or_(f"sync_blocked_until.is.null,sync_blocked_until.lt.{now_iso}")
            .or_(f"next_sync_at.is.null,next_sync_at.lte.{now_iso}")
            .order("sync_priority", desc=False)
            .order("next_sync_at", desc=False)
            .limit(self._config.BATCH_SIZE)
        )

        resp = query.execute()
        cases = resp.data or []

        now = datetime.now(_TZ)
        if _is_office_hours(now):
            cases = [c for c in cases if c.get("sync_priority") is None or c["sync_priority"] <= 2]
        elif not _is_archived_window(now):
            cases = [c for c in cases if c.get("sync_priority") is None or c["sync_priority"] <= 3]

        if not cases:
            return []

        case_ids = [c["id"] for c in cases]
        (
            self._sb.from_("cases")
            .update({"sync_worker_id": self._config.WORKER_ID})
            .in_("id", case_ids)
            .execute()
        )

        logger.info("Claimed batch of %d cases", len(cases))
        return cases

    async def release_batch(self, case_ids: list[str]):
        if not case_ids:
            return
        (
            self._sb.from_("cases")
            .update({"sync_worker_id": None})
            .in_("id", case_ids)
            .execute()
        )
