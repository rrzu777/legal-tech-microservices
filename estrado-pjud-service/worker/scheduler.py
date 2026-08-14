import asyncio
import logging
from datetime import datetime

from worker.config import WorkerConfig, TZ_SANTIAGO, run_query

logger = logging.getLogger(__name__)

RELEASE_SCHEMA_CACHE_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


def _postgrest_error_code(error: Exception) -> str | None:
    code = getattr(error, "code", None)
    if code is not None:
        return str(code)
    if error.args and isinstance(error.args[0], dict):
        value = error.args[0].get("code")
        return str(value) if value is not None else None
    return None


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
        validation_once = self._config.PJUD_OFF_HOURS_VALIDATION_ONCE is True
        if not validation_once and not is_scheduled_processing_window(now):
            return []
        if now.tzinfo is None:
            now = now.replace(tzinfo=TZ_SANTIAGO)
        now_iso = now.isoformat()

        # La selección y el claim viven en una sola transacción PostgreSQL con
        # FOR UPDATE SKIP LOCKED. Un SELECT seguido de UPDATE permitía que dos
        # procesos pagaran la misma causa al abrir la ventana de las 08:00.
        resp = await run_query(self._sb.rpc("claim_pjud_sync_cases", {
            "p_worker_id": self._config.WORKER_ID,
            "p_limit": 1 if validation_once else self._config.BATCH_SIZE,
            "p_now": now_iso,
        }))
        cases = resp.data or []

        if not cases:
            return []

        logger.info("Claimed batch of %d cases", len(cases))
        return cases

    async def _release_batch_via_rpc(self, case_ids: list[str]):
        return await run_query(self._sb.rpc("release_pjud_sync_claims", {
            "p_worker_id": self._config.WORKER_ID,
            "p_case_ids": case_ids,
        }))

    async def release_batch(self, case_ids: list[str]):
        if not case_ids:
            return

        try:
            await self._release_batch_via_rpc(case_ids)
            return
        except Exception as rpc_error:
            # Deploy this worker before migration 00074. Only a missing RPC is
            # allowed to use the old CAS release; auth/DB failures stay loud.
            if _postgrest_error_code(rpc_error) != "PGRST202":
                raise

        try:
            await run_query(
                self._sb.from_("cases")
                .update({"sync_worker_id": None, "sync_claimed_at": None})
                .in_("id", case_ids)
                .eq("sync_worker_id", self._config.WORKER_ID)
            )
        except Exception as direct_error:
            # The migration may have landed after the first schema-cache miss
            # but before the direct UPDATE. PostgREST reloads asynchronously
            # after commit, so keep the claimed IDs in memory while its schema
            # cache converges instead of crashing on one immediate retry.
            for delay in (0.0, *RELEASE_SCHEMA_CACHE_RETRY_DELAYS):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await self._release_batch_via_rpc(case_ids)
                    return
                except Exception as retry_error:
                    if _postgrest_error_code(retry_error) != "PGRST202":
                        raise
            raise direct_error
