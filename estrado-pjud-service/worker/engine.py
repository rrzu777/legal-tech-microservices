# worker/engine.py
import asyncio
import logging
from datetime import datetime, timedelta, date

from app.parsers.normalizer import parse_case_identifier
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.parsers.detail_parser import parse_detail
from worker.config import WorkerConfig, TZ_SANTIAGO, run_query

logger = logging.getLogger(__name__)
_BLOCK_DURATION_S = 3600  # 1 hour

MATTER_TO_COMPETENCIA = {
    "civil": "civil",
    "laboral": "laboral",
    "cobranza": "cobranza",
    "suprema": "suprema",
    "apelaciones": "apelaciones",
    "penal": "penal",
}

SYNC_INTERVALS_HOURS = {
    1: 4,    # hot
    2: 12,   # warm
    3: 24,   # cold
    4: 168,  # archived (weekly)
}

TRAMITE_TO_TYPE = {
    "Resolucion": "resolution",
    "Resolución": "resolution",
    "Escrito": "filing",
    "Actuacion Receptor": "notification",
    "Actuación Receptor": "notification",
}


def _map_tramite(tramite: str) -> str:
    for key, value in TRAMITE_TO_TYPE.items():
        if key in tramite:
            return value
    return "other"


def _compute_priority(case_status: str, latest_date: str | None) -> int:
    if case_status in ("closed", "archived"):
        return 4
    if not latest_date:
        return 2
    try:
        d = date.fromisoformat(latest_date)
        today = datetime.now(TZ_SANTIAGO).date()
        days = (today - d).days
    except ValueError:
        return 2
    if days < 7:
        return 1
    if days <= 30:
        return 2
    return 3


def _compute_next_sync_at(priority: int) -> str:
    hours = SYNC_INTERVALS_HOURS.get(priority, 24)
    return (datetime.now(TZ_SANTIAGO) + timedelta(hours=hours)).isoformat()


def _get_latest_movement_date(movements: list[dict]) -> str | None:
    dates = sorted(
        [m["fecha"] for m in movements if m.get("fecha")],
        reverse=True,
    )
    return dates[0] if dates else None


def _build_external_movement_key(case_number: str, cuaderno: str, folio) -> str:
    return f"{case_number}:{cuaderno}:{folio}"


async def search_pjud_via_session(session, competencia: str, form_data: dict, timeout: float) -> dict:
    """Call OJV search via session and parse results."""
    html = await asyncio.wait_for(
        session.search(competencia, form_data),
        timeout=timeout,
    )
    blocked = detect_blocked(html)
    if blocked:
        return {"found": False, "match_count": 0, "matches": [], "blocked": True, "error": None}
    matches = parse_search_results(html, competencia)
    return {
        "found": len(matches) > 0,
        "match_count": len(matches),
        "matches": matches,
        "blocked": False,
        "error": None,
    }


async def detail_pjud_via_session(session, competencia: str, detail_key: str, timeout: float) -> dict:
    """Call OJV detail via session and parse results."""
    html = await asyncio.wait_for(
        session.detail(competencia, detail_key),
        timeout=timeout,
    )
    if len(html.strip()) < 100:
        return {"metadata": {}, "movements": [], "litigantes": [], "blocked": True, "error": None}
    parsed = parse_detail(html)
    return {**parsed, "blocked": False, "error": None}


class SyncEngine:
    def __init__(self, pool, supabase, notifier, metrics, backoff, config: WorkerConfig):
        self._pool = pool
        self._sb = supabase
        self._notifier = notifier
        self._metrics = metrics
        self._backoff = backoff
        self._config = config

    async def sync_case(self, case: dict) -> dict:
        started_at = datetime.now(TZ_SANTIAGO)

        # Create sync run
        sync_run_id = None
        try:
            resp = await run_query(
                self._sb.from_("case_sync_runs")
                .insert({
                    "law_firm_id": case["law_firm_id"],
                    "case_id": case["id"],
                    "status": "running",
                    "trigger": "scheduled_sync",
                    "started_at": started_at.isoformat(),
                })
                .select("id")
                .single()
            )
            sync_run_id = resp.data.get("id") if resp.data else None
        except Exception:
            logger.exception("Failed to create sync_run")

        session = None
        try:
            # Parse identifier — raises ValueError on invalid input
            try:
                parsed = parse_case_identifier(case["case_number"])
            except ValueError:
                await self._finish_run(sync_run_id, started_at, "error", 0, "Invalid identifier")
                await self._update_case_error(case["id"], "Identificador invalido", case.get("sync_attempts", 0))
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            competencia = MATTER_TO_COMPETENCIA.get(case.get("matter", ""))
            if not competencia:
                await self._finish_run(sync_run_id, started_at, "error", 0, "Unsupported matter")
                await self._update_case_error(case["id"], "Materia no soportada", case.get("sync_attempts", 0))
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            session = await self._pool.acquire()
            await self._pool.enforce_global_rate_limit()

            # Build search form data (mirrors routes/search.py)
            # For apelaciones, read corte from the case's external_payload.
            # Falls back to empty string if not available (searches all cortes).
            corte_value = ""
            if competencia == "apelaciones":
                corte_value = str(
                    case.get("external_payload", {}).get("corte", "") if case.get("external_payload") else ""
                )
                if not corte_value:
                    logger.warning(
                        "No corte in external_payload for apelaciones case %s; searching all cortes",
                        case.get("case_number", case["id"]),
                    )

            form_data = {
                "action": "search",
                "competencia": competencia,
                "conRolCausa": parsed["numero"],
                "conEraCausa": parsed["anno"],
                "conCorte": corte_value,
                "conTribunal": "",
                "conTipoBusApe": "",
                "radio-groupPenal": "",
                "radio-group": "",
                "ruc1": "",
                "ruc2": "",
                "rucPen1": "",
                "rucPen2": "",
                "conCaratulado": "",
                "g-recaptcha-response-rit": "",
            }

            if competencia == "suprema":
                form_data["conTipoBus"] = "0"
            else:
                form_data["conTipoCausa"] = parsed["tipo"]

            # Search
            search_result = await search_pjud_via_session(
                session, competencia, form_data, self._config.OJV_TIMEOUT_S,
            )

            if search_result["blocked"]:
                await self._finish_run(sync_run_id, started_at, "blocked", 0, "Blocked by OJV")
                await self._update_case_blocked(case["id"])
                self._backoff.record_blocked()
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            if not search_result["found"]:
                await self._finish_run(sync_run_id, started_at, "error", 0, "Not found in OJV")
                await self._update_case_error(case["id"], "No encontrada en OJV", case.get("sync_attempts", 0))
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            # Get detail key
            detail_key = case.get("external_case_key") or search_result["matches"][0].get("key")
            if not detail_key:
                await self._finish_run(sync_run_id, started_at, "error", 0, "No detail key available")
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            await self._pool.enforce_global_rate_limit()

            # Detail
            detail = await detail_pjud_via_session(
                session, competencia, detail_key, self._config.OJV_TIMEOUT_S,
            )

            if detail["blocked"]:
                await self._finish_run(sync_run_id, started_at, "blocked", 0, "Detail blocked")
                await self._update_case_blocked(case["id"])
                self._backoff.record_blocked()
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            # Upsert movements
            new_count = await self._upsert_movements(case, detail)

            # Update case
            latest_date = _get_latest_movement_date(detail["movements"])
            priority = _compute_priority(case.get("status", "active"), latest_date)
            next_sync = _compute_next_sync_at(priority)

            canonical = f"{competencia}:{parsed['tipo']}:{parsed['numero']}:{parsed['anno']}"

            await run_query(
                self._sb.from_("cases").update({
                    "tracking_status": "active",
                    "last_sync_at": datetime.now(TZ_SANTIAGO).isoformat(),
                    "last_sync_status": "success",
                    "last_sync_error": None,
                    "sync_attempts": (case.get("sync_attempts") or 0) + 1,
                    "canonical_identifier": canonical,
                    "external_case_key": case.get("external_case_key") or detail_key,
                    "external_payload": {
                        "metadata": detail["metadata"],
                        "litigantes": detail["litigantes"],
                    },
                    "sync_priority": priority,
                    "next_sync_at": next_sync,
                    "latest_movement_date": latest_date,
                }).eq("id", case["id"])
            )

            # Finish sync run
            await self._finish_run(sync_run_id, started_at, "success", new_count)

            # Notify if new movements
            if new_count > 0:
                await self._notifier.notify_new_movements(case, new_count)

            self._backoff.record_success()
            self._metrics.record_sync()
            logger.info("Synced case %s: %d new movements", case["case_number"], new_count)
            return {"success": True, "new_movements": new_count}

        except asyncio.TimeoutError:
            msg = "Timeout al consultar OJV"
            logger.warning("Timeout syncing case %s", case["case_number"])
            await self._finish_run(sync_run_id, started_at, "error", 0, msg)
            await self._update_case_error(case["id"], msg, case.get("sync_attempts", 0))
            self._backoff.record_failure()
            self._metrics.record_error()
            return {"success": False, "new_movements": 0}

        except Exception as e:
            msg = str(e)
            logger.exception("Error syncing case %s", case["case_number"])
            await self._finish_run(sync_run_id, started_at, "error", 0, msg)
            await self._update_case_error(case["id"], msg, case.get("sync_attempts", 0))
            self._backoff.record_failure()
            self._metrics.record_error()
            return {"success": False, "new_movements": 0}

        finally:
            if session:
                self._pool.release(session)

    async def _upsert_movements(self, case: dict, detail: dict) -> int:
        movements = detail.get("movements", [])
        if not movements:
            return 0

        rows = []
        for mov in movements:
            rows.append({
                "law_firm_id": case["law_firm_id"],
                "case_id": case["id"],
                "date": mov.get("fecha"),
                "title": f"{mov.get('tramite', '')}: {mov.get('descripcion', '')}",
                "description": f"Cuaderno: {mov.get('cuaderno', '')} | Folio: {mov.get('folio', '')} | Etapa: {mov.get('etapa', '')}",
                "movement_type": _map_tramite(mov.get("tramite", "")),
                "source": "sync",
                "document_url": mov.get("documento_url"),
                "is_relevant": True,
                "include_in_report": True,
                "external_movement_key": _build_external_movement_key(
                    case["case_number"],
                    mov.get("cuaderno", ""),
                    mov.get("folio", ""),
                ),
                "raw_payload": mov,
            })

        # Count before
        before_resp = await run_query(
            self._sb.from_("case_movements")
            .select("id", count="exact")
            .eq("case_id", case["id"])
        )
        before_count = before_resp.count if before_resp.count is not None else 0

        # Upsert (ignore duplicates)
        await run_query(
            self._sb.from_("case_movements").upsert(
                rows,
                on_conflict="case_id,external_movement_key",
                ignore_duplicates=True,
            )
        )

        # Count after
        after_resp = await run_query(
            self._sb.from_("case_movements")
            .select("id", count="exact")
            .eq("case_id", case["id"])
        )
        after_count = after_resp.count if after_resp.count is not None else 0

        return after_count - before_count

    async def _finish_run(self, run_id, started_at, status, new_movements, error=None):
        if not run_id:
            return
        now = datetime.now(TZ_SANTIAGO)
        duration_ms = int((now - started_at).total_seconds() * 1000)
        try:
            await run_query(
                self._sb.from_("case_sync_runs").update({
                    "status": status,
                    "finished_at": now.isoformat(),
                    "duration_ms": duration_ms,
                    "new_movements_count": new_movements,
                    "error_message": error,
                }).eq("id", run_id)
            )
        except Exception:
            logger.exception("Failed to finish sync_run %s", run_id)

    async def _update_case_blocked(self, case_id: str):
        blocked_until = (datetime.now(TZ_SANTIAGO) + timedelta(seconds=_BLOCK_DURATION_S)).isoformat()
        await run_query(
            self._sb.from_("cases").update({
                "tracking_status": "blocked",
                "last_sync_status": "blocked",
                "last_sync_error": "Acceso bloqueado por OJV",
                "sync_blocked_until": blocked_until,
            }).eq("id", case_id)
        )

    async def _update_case_error(self, case_id: str, error: str, sync_attempts: int = 0):
        """Update case with error status and escalating backoff.

        Backoff schedule based on sync_attempts:
          1st error: 5 minutes
          2nd error: 30 minutes
          3rd error: 2 hours
          4th+: 6 hours
        """
        backoff_seconds = {0: 300, 1: 1800, 2: 7200}.get(
            sync_attempts, 21600
        )
        blocked_until = (datetime.now(TZ_SANTIAGO) + timedelta(seconds=backoff_seconds)).isoformat()

        await run_query(
            self._sb.from_("cases").update({
                "tracking_status": "error",
                "last_sync_status": "error",
                "last_sync_error": error,
                "sync_blocked_until": blocked_until,
            }).eq("id", case_id)
        )
