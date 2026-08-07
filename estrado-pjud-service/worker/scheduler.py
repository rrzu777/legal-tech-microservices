import logging
from datetime import datetime

from worker.config import WorkerConfig, TZ_SANTIAGO, run_query

logger = logging.getLogger(__name__)


def is_scheduled_processing_window(dt: datetime | None = None) -> bool:
    now = dt or datetime.now(TZ_SANTIAGO)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ_SANTIAGO)
    else:
        now = now.astimezone(TZ_SANTIAGO)
    return now.weekday() < 5 and 8 <= now.hour < 18

class Scheduler:
    def __init__(self, config: WorkerConfig, supabase):
        self._config = config
        self._sb = supabase

    async def verify_claim_contract(self, now: datetime | None = None) -> None:
        """Verifica el RPC sin reclamar causas, antes de mintear el pool."""
        now = now or datetime.now(TZ_SANTIAGO)
        await run_query(self._sb.rpc("claim_pjud_sync_cases", {
            "p_worker_id": self._config.WORKER_ID,
            "p_limit": 0,
            "p_now": now.isoformat(),
        }))

    async def get_next_batch(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(TZ_SANTIAGO)
        if not is_scheduled_processing_window(now):
            return []
        if now.tzinfo is None:
            now = now.replace(tzinfo=TZ_SANTIAGO)
        now_iso = now.isoformat()

        # La selección y el claim viven en una sola transacción PostgreSQL con
        # FOR UPDATE SKIP LOCKED. Un SELECT seguido de UPDATE permitía que dos
        # procesos pagaran la misma causa al abrir la ventana de las 08:00.
        resp = await run_query(self._sb.rpc("claim_pjud_sync_cases", {
            "p_worker_id": self._config.WORKER_ID,
            "p_limit": self._config.BATCH_SIZE,
            "p_now": now_iso,
        }))
        cases = resp.data or []

        if not cases:
            return []

        logger.info("Claimed batch of %d cases", len(cases))
        return cases

    async def release_batch(self, case_ids: list[str]):
        if not case_ids:
            return
        await run_query(
            self._sb.from_("cases")
            .update({"sync_worker_id": None, "sync_claimed_at": None})
            .in_("id", case_ids)
            .eq("sync_worker_id", self._config.WORKER_ID)
        )
