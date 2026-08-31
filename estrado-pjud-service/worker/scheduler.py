import re
import logging
import time
from datetime import datetime

from worker.config import WorkerConfig, TZ_SANTIAGO, run_query

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_S = 15 * 60


def is_scheduled_processing_window(dt: datetime | None = None) -> bool:
    now = dt or datetime.now(TZ_SANTIAGO)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ_SANTIAGO)
    else:
        now = now.astimezone(TZ_SANTIAGO)
    return now.weekday() < 5 and 8 <= now.hour < 18


def is_processing_allowed(
    dt: datetime | None = None,
    *,
    validation_once: bool = False,
    process_outside_office_hours: bool = False,
) -> bool:
    """Centraliza el bypass explícito sin alterar los límites del scheduler."""
    return (
        validation_once
        or process_outside_office_hours
        or is_scheduled_processing_window(dt)
    )


class Scheduler:
    def __init__(self, config: WorkerConfig, supabase):
        self._config = config
        self._sb = supabase
        self._last_reconciliation_monotonic = float("-inf")

    async def reconcile_stale_runs(self, *, force: bool = False) -> dict | None:
        """Reconcile stale run rows without accepting a case or cutoff input."""
        now = time.monotonic()
        if (
            not force
            and now - self._last_reconciliation_monotonic < RECONCILE_INTERVAL_S
        ):
            return None
        response = await run_query(
            self._sb.rpc("reconcile_stale_pjud_sync_runs", {})
        )
        rows = response.data if isinstance(response.data, list) else []
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise RuntimeError("sync_run_reconciliation_unavailable")
        result = rows[0]
        expected_fields = {"reconciled_count", "historical_unowned_count"}
        if set(result) != expected_fields or any(
            isinstance(result[field], bool)
            or not isinstance(result[field], int)
            or result[field] < 0
            for field in expected_fields
        ):
            raise RuntimeError("sync_run_reconciliation_invalid")
        self._last_reconciliation_monotonic = now
        return result

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
        process_outside_office_hours = (
            self._config.PJUD_PROCESS_OUTSIDE_OFFICE_HOURS is True
        )
        if not is_processing_allowed(
            now,
            validation_once=validation_once,
            process_outside_office_hours=process_outside_office_hours,
        ):
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

    async def release_batch(self, claims: list[dict]):
        """Release only the originally captured identities; no token fetch/fallback."""
        if not isinstance(claims, list) or len(claims) > 100:
            raise ValueError("invalid_pjud_release_claims")
        captured = []
        seen = set()
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) != {"case_id", "claim_token"}:
                raise ValueError("invalid_pjud_release_claims")
            if any(not isinstance(value, str) or re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value,
            ) is None for value in claim.values()):
                raise ValueError("invalid_pjud_release_claims")
            case_id = claim["case_id"].lower()
            if case_id in seen:
                raise ValueError("invalid_pjud_release_claims")
            seen.add(case_id)
            captured.append(dict(claim))
        if not captured:
            return
        await run_query(self._sb.rpc("release_pjud_sync_claims_v2", {
            "p_worker_id": self._config.WORKER_ID, "p_claims": captured,
        }))
