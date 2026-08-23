# worker/engine.py
import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, date

import httpx
from pydantic import SecretStr

from app.alerting import send_ops_alert
from app.anexo_endpoints import ANEXO_ENDPOINTS
from app.catalogs import CatalogService, normalize_catalog_label
from app.document_downloader import download_documents, download_single_document
from app.document_metadata import (
    extract_pjud_document_sources,
    sanitize_pjud_case_external_payload,
    sanitize_pjud_movement_payload,
)
from app.errors import safe_error
from app.failure_kind import (
    UpstreamChangedError,
    block_cause,
    classify_exception,
    reject_empty_body,
    slot_still_healthy,
)
from app.familia.auth import FamiliaAuthSession, FamiliaBlockedError, InvalidCredentialsError
from app.familia.parser import parse_familia_results
from app.ojv.errors import OjvSessionError
from app.matching import is_definitive_not_found, matches_requested_candidate, rank_matches
from app.models import CandidateMatch, SearchRequest
from app.parsers.anexo_parser import parse_anexo_list
from app.parsers.form_builder import build_search_form_data
from app.parsers.normalizer import parse_case_identifier, competencia_path
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.parsers.detail_parser import parse_detail
from app.proxy_billing import ProxyBillingExhaustedError, is_proxy_billing_error
from app.proxy_cost import (
    ProxyBudgetExceededError,
    ProxyUsagePersistenceError,
    is_proxy_cost_control_error,
)
from app.r2 import R2Client
from worker.config import WorkerConfig, TZ_SANTIAGO, run_query
from worker.import_jobs import ImportDiscoveryWorker
from worker.sync_messages import BlockCause, blocked_error_message
from worker.proxy_control import ProxyControl
from worker.proxy_usage import (
    DEFAULT_PRICE_PER_GB_USD,
    ProxyUsageTracker,
)
from worker.session_pool import ReleaseDisposition

logger = logging.getLogger(__name__)
_BLOCK_DURATION_S = 3600  # 1 hour
# Un parse-failure reintenta en ~1h (no cada ciclo) y las alertas se agrupan a
# lo más 1 por hora, para no spamear Telegram ante un drift global del parser.
_PARSE_RETRY_S = 3600
_PARSE_ALERT_COOLDOWN_S = 3600
_MOVEMENT_PAGE_SIZE = 1_000
_DOCUMENT_SOURCE_TIMEOUT_S = 2.0
_MAX_LOOKUP_CANDIDATES = 100
_BEGIN_SYNC_RUN_REPLAY_DELAYS_S = (0.1,)
# Read/protocol failures can lose a committed response; write failures can occur
# after partial transmission. Connect and pool-acquisition failures are pre-send
# and deliberately excluded from the idempotent replay path.
_AMBIGUOUS_BEGIN_SYNC_RUN_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
)
_SYNC_RUN_ERROR_CODES = frozenset({
    "worker_interrupted",
    "sync_run_unavailable",
    "invalid_identifier",
    "unsupported_matter",
    "case_not_found",
    "parse_failed",
    "pjud_timeout",
    "upstream_changed",
    "remote_protocol_disconnect",
    "ojv_blocked",
    "infra_unavailable",
    "credential_unavailable",
    "unknown_case_error",
})
_SYNC_RUN_EXACT_ERROR_TOKENS = {
    "invalid identifier": "invalid_identifier",
    "invalid_identity": "invalid_identifier",
    "unsupported matter": "unsupported_matter",
    "auth_type no soportado": "unsupported_matter",
    "not found in ojv": "case_not_found",
    "case not found in familia portal": "case_not_found",
    "parse_failed": "parse_failed",
    "no detail key available": "parse_failed",
    "blocked by ojv": "ojv_blocked",
    "detail blocked": "ojv_blocked",
    "missing ojv_credential_id": "credential_unavailable",
    "credential inactive or missing": "credential_unavailable",
    "invalid credentials": "credential_unavailable",
    "pool sin bundle f5": "infra_unavailable",
}
_LOOKUP_CANDIDATE_FIELDS = (
    "rol", "ruc", "tribunal", "caratulado", "fecha_ingreso",
    "tribunal_code", "corte", "corte_code", "libro", "libro_code",
)


class InvalidCanonicalIdentityError(ValueError):
    """Persisted v2 identity is present but violates PJUD's v2 contract."""


class ImportCredentialInfrastructureError(RuntimeError):
    """The app credential boundary is temporarily unavailable."""


def _release_disposition_for_error(
    error: BaseException,
    *,
    transport_revalidation_enabled: bool,
) -> ReleaseDisposition:
    if isinstance(error, OjvSessionError) and error.code.value == "upstream_changed":
        # Markup drift is independent of residential egress. A paid remint
        # cannot repair it and would only retry a terminal contract failure.
        return "healthy"
    if transport_revalidation_enabled and isinstance(
        error, httpx.RemoteProtocolError,
    ):
        return "validate_before_reuse"
    return "healthy" if slot_still_healthy(error) else "replace_before_reuse"


def _sync_run_error_code(
    error: BaseException | str | None,
    *,
    failure_kind: str | None = None,
) -> str | None:
    """Map worker outcomes to the database's closed scheduled-run taxonomy."""
    if error is None:
        return None
    if isinstance(error, OjvSessionError):
        return {
            "upstream_changed": "upstream_changed",
            "timeout": "pjud_timeout",
            "waf": "ojv_blocked",
            "session_expired": "infra_unavailable",
            "credential_invalid": "credential_unavailable",
        }[error.code.value]
    if isinstance(error, httpx.RemoteProtocolError):
        return "remote_protocol_disconnect"
    if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "pjud_timeout"
    if isinstance(error, UpstreamChangedError):
        return "upstream_changed"
    if isinstance(error, InvalidCanonicalIdentityError):
        return "invalid_identifier"

    normalized = safe_error(error).strip().lower()
    if normalized in _SYNC_RUN_ERROR_CODES:
        return normalized
    exact_code = _SYNC_RUN_EXACT_ERROR_TOKENS.get(normalized)
    if exact_code is not None:
        return exact_code
    if failure_kind == "infra":
        return "infra_unavailable"
    if failure_kind == "ojv":
        return "ojv_blocked"
    return "unknown_case_error"

MATTER_TO_COMPETENCIA = {
    "civil": "civil",
    "laboral": "laboral",
    "cobranza": "cobranza",
    "suprema": "suprema",
    "apelaciones": "apelaciones",
    "penal": "penal",
}

SYNC_INTERVALS_HOURS = {
    1: 6,    # urgente explícita: hasta dos veces por día hábil
    2: 24,   # diaria: movimiento durante los últimos 30 días
    3: 168,  # semanal: sin movimiento durante más de 30 días
    4: 168,  # archivada (semanal nominal; fuera del polling activo)
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


def _compute_priority(
    case_status: str,
    latest_date: str | None,
    *,
    is_urgent: bool = False,
) -> int:
    if case_status in ("closed", "archived"):
        return 4
    if is_urgent:
        return 1
    if not latest_date:
        return 2
    try:
        d = date.fromisoformat(latest_date)
        today = datetime.now(TZ_SANTIAGO).date()
        days = (today - d).days
    except ValueError:
        return 2
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


_NULL_FOLIO_KEY_FIELDS = ("fecha", "cuaderno", "tramite", "descripcion")
_PRESENT_FOLIO_DEDUPE_FIELDS = (
    "fecha",
    "cuaderno",
    "tramite",
    "descripcion",
    "etapa",
    "foja",
)

_PRIMARY_DOCUMENT_FIELDS = (
    "documento_url",
    "documento_token",
    "documento_param",
)
_ANEXO_FIELDS = ("anexo_func", "anexo_token")
_CANONICAL_WHITESPACE_RE = re.compile(
    r"[\u0009-\u000D\u0020\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000\uFEFF]+"
)


def _document_source_id(kind: str, *parts: object) -> str:
    """Stable document identity that deliberately excludes expiring PJUD JWTs."""
    canonical = json.dumps(
        [kind, *("" if part is None else str(part).strip() for part in parts)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _movement_identity(movement: dict) -> tuple:
    """Return the stable PJUD fields that identify one logical movement."""
    folio = movement.get("folio")
    fields = (
        _NULL_FOLIO_KEY_FIELDS
        if folio is None
        else _PRESENT_FOLIO_DEDUPE_FIELDS
    )
    return (
        _normalize_movement_identity_part(folio),
        *(
            _normalize_movement_identity_part(movement.get(field))
            for field in fields
        ),
    )


def _normalize_movement_identity_part(value) -> str:
    normalized = unicodedata.normalize(
        "NFKC", str("" if value is None else value)
    )
    normalized = _CANONICAL_WHITESPACE_RE.sub(" ", normalized)
    if normalized.startswith(" "):
        normalized = normalized[1:]
    if normalized.endswith(" "):
        normalized = normalized[:-1]
    return normalized


def _movement_sort_key(movement: dict) -> tuple[int, str]:
    """Keep the primary row first and make sibling suffixes order-independent."""
    has_primary_fields = bool(
        _normalize_movement_identity_part(movement.get("tramite"))
        or _normalize_movement_identity_part(movement.get("etapa"))
    )
    encoded_identity = json.dumps(
        list(_movement_identity(movement)),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return (0 if has_primary_fields else 1, hashlib.sha256(encoded_identity).hexdigest())


def _is_movement_key_for_base(key: str, base_key: str) -> bool:
    if key == base_key:
        return True
    suffix = key[len(base_key):] if key.startswith(base_key) else ""
    return suffix.startswith("#") and suffix[1:].isdigit()


def _external_key_rank(key: str, base_key: str) -> int:
    return 1 if key == base_key else int(key[len(base_key) + 1:])


def _build_movement_external_key(case_number: str, movement: dict) -> str:
    """Build a stable movement key while retaining every historical folio key."""
    folio = movement.get("folio")
    if folio is not None:
        return _build_external_movement_key(
            case_number,
            movement.get("cuaderno", ""),
            folio,
        )

    identity = {
        field: _normalize_movement_identity_part(movement.get(field))
        for field in _NULL_FOLIO_KEY_FIELDS
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"pjud:null-folio:{hashlib.sha256(encoded).hexdigest()}"


def _copy_movement(movement: dict) -> dict:
    copied = dict(movement)
    copied["documentos_adicionales"] = [
        dict(document)
        for document in movement.get("documentos_adicionales", [])
    ]
    return copied


def _prefer_complete_group(target: dict, source: dict, fields: tuple[str, ...]) -> None:
    """Copy one linked metadata group only when the source is more complete.

    URL/token/param and anexo function/token are paired values. Filling them
    independently can manufacture a credential combination PJUD never emitted.
    """
    target_score = sum(bool(target.get(field)) for field in fields)
    source_score = sum(bool(source.get(field)) for field in fields)
    if source_score <= target_score:
        return
    for field in fields:
        target[field] = source.get(field)


def _merge_movement_metadata(target: dict, source: dict) -> None:
    """Keep the richest document metadata from duplicate logical movements."""
    _prefer_complete_group(target, source, _PRIMARY_DOCUMENT_FIELDS)
    _prefer_complete_group(target, source, _ANEXO_FIELDS)

    additional = target.setdefault("documentos_adicionales", [])
    seen_documents = {
        (document.get("url"), document.get("token"), document.get("param"))
        for document in additional
    }
    for document in source.get("documentos_adicionales", []):
        identity = (
            document.get("url"),
            document.get("token"),
            document.get("param"),
        )
        if identity not in seen_documents:
            additional.append(dict(document))
            seen_documents.add(identity)


def _prepare_pjud_movements(
    case: dict,
    movements: list[dict],
    *,
    log_undated: bool,
    existing_movements: list[dict] | None = None,
) -> list[tuple[dict, str]]:
    """Filter invalid rows, collapse logical duplicates and assign persisted keys."""
    skipped_count = 0
    groups: dict[str, dict[tuple, dict]] = {}

    for movement in movements:
        if not movement.get("fecha"):
            skipped_count += 1
            continue

        base_key = _build_movement_external_key(case["case_number"], movement)
        identity = _movement_identity(movement)
        group = groups.setdefault(base_key, {})
        existing = group.get(identity)
        if existing is not None:
            _merge_movement_metadata(existing, movement)
            continue
        group[identity] = _copy_movement(movement)

    if skipped_count and log_undated:
        logger.warning(
            "Skipping undated PJUD movements",
            extra={
                "case_id": case.get("id"),
                "case_number": case.get("case_number"),
                "skipped_count": skipped_count,
            },
        )

    existing_keys_by_identity: dict[tuple[str, tuple], list[str]] = {}
    reserved_keys_by_base: dict[str, set[str]] = {}
    for existing in existing_movements or []:
        external_key = existing.get("external_movement_key")
        raw_payload = existing.get("raw_payload")
        if not external_key or not isinstance(raw_payload, dict):
            continue
        base_key = _build_movement_external_key(case["case_number"], raw_payload)
        if not _is_movement_key_for_base(external_key, base_key):
            continue
        identity_key = (base_key, _movement_identity(raw_payload))
        existing_keys_by_identity.setdefault(identity_key, []).append(external_key)
        reserved_keys_by_base.setdefault(base_key, set()).add(external_key)

    prepared: list[tuple[dict, str]] = []
    for base_key, group in groups.items():
        logical_movements = sorted(group.values(), key=_movement_sort_key)
        reserved_keys = reserved_keys_by_base.get(base_key, set())
        assigned_keys: set[str] = set()
        for movement in logical_movements:
            identity_key = (base_key, _movement_identity(movement))
            historical_key = next((
                key
                for key in sorted(
                    existing_keys_by_identity.get(identity_key, []),
                    key=lambda value: _external_key_rank(value, base_key),
                )
                if key not in assigned_keys
            ), None)
            external_key = historical_key
            if external_key is None:
                suffix = 1
                while True:
                    candidate = base_key if suffix == 1 else f"{base_key}#{suffix}"
                    suffix += 1
                    if candidate not in reserved_keys and candidate not in assigned_keys:
                        external_key = candidate
                        break
            assigned_keys.add(external_key)
            prepared.append((movement, external_key))

    return prepared


def search_params_from_case(case: dict) -> dict:
    """Return the persisted PJUD identity without applying legacy defaults.

    A case row from before migration 00063 has no ``tribunal_unknown`` column.
    Its absence is the staged-rollout marker: callers keep the v1 path rather
    than turning an already-tracked case into an invalid v2 request.
    """
    return {
        "case_type": case.get("case_type"),
        "case_number": case.get("case_number"),
        "corte": case.get("court_code"),
        "tribunal": case.get("tribunal_code"),
        "libro": case.get("libro"),
        "search_mode": case.get("pjud_search_mode"),
        "allow_broad": bool(case.get("tribunal_unknown")),
    }


def _canonical_search_request(case: dict, competencia: str) -> SearchRequest | None:
    """Build the v2 contract only for rows that carry the new schema marker.

    Rows that are missing it (or have not yet been backfilled with a complete
    identity) remain v1-compatible until JurisTrack's migration is deployed.
    """
    if "tribunal_unknown" not in case:
        return None

    try:
        return SearchRequest(
            contract_version=2,
            competencia=competencia,
            **search_params_from_case(case),
        )
    except ValueError as exc:
        canonical_signal = (
            case.get("pjud_search_mode") is not None
            or case.get("tribunal_code") is not None
            or case.get("tribunal_unknown") is True
            or (competencia != "apelaciones" and case.get("court_code") is not None)
        )
        if canonical_signal:
            raise InvalidCanonicalIdentityError(str(exc)) from exc
        logger.info(
            "Case %s has incomplete canonical PJUD identity; retaining v1 compatibility",
            case.get("id"),
        )
        return None


def _matches_known_context(candidate: CandidateMatch, request: SearchRequest) -> bool:
    """Known v2 context requires complete, matching official codes."""
    tribunal_matches = request.tribunal is None or candidate.tribunal_code == request.tribunal
    corte_matches = request.corte is None or candidate.corte_code == request.corte
    return tribunal_matches and corte_matches


def _confirmed_match(matches: list[dict], request: SearchRequest) -> CandidateMatch | None:
    """Return one exact official candidate, never an arbitrary result row."""
    candidates = _dedupe_candidates([CandidateMatch.model_validate(match) for match in matches])
    ranked = rank_matches(candidates, request)
    exact = [
        candidate
        for candidate in ranked.matches
        if matches_requested_candidate(candidate, request)
        and _matches_known_context(candidate, request)
    ]
    return exact[0] if len(exact) == 1 else None


def _dedupe_candidates(candidates: list[CandidateMatch]) -> list[CandidateMatch]:
    """Collapse repeated PJUD rows before exact-match ambiguity is evaluated."""
    unique: list[CandidateMatch] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for candidate in candidates:
        identity = (
            normalize_catalog_label(candidate.rol),
            normalize_catalog_label(candidate.ruc or ""),
            str(candidate.corte_code) if candidate.corte_code is not None else normalize_catalog_label(candidate.corte or ""),
            str(candidate.tribunal_code) if candidate.tribunal_code is not None else normalize_catalog_label(candidate.tribunal),
            candidate.libro_code or normalize_catalog_label(candidate.libro or ""),
        )
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)
    return unique


def _dedupe_movement_keys(keys: list[str]) -> list[str]:
    """Desambigua claves repetidas DENTRO de un mismo batch de upsert.

    La clave es `case_number:cuaderno:folio` y un mismo folio puede tener mas de un
    tramite, asi que dos filas del mismo batch colisionan. Postgres rechaza el upsert
    ENTERO con 21000 ("ON CONFLICT DO UPDATE command cannot affect row a second
    time"), no solo la fila repetida: eso dejo a T-100-2024 sin sincronizar desde el
    13 de marzo.

    La PRIMERA ocurrencia conserva la clave de siempre — si cambiara, las filas ya
    guardadas dejarian de matchear y se reinsertarian como movimientos nuevos,
    disparando notificaciones falsas en todas las causas — y las siguientes llevan
    sufijo. No se descarta ninguna: perder un movimiento de una causa judicial en
    silencio es peor que el bug original.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for key in keys:
        n = seen.get(key, 0) + 1
        seen[key] = n
        out.append(key if n == 1 else f"{key}#{n}")
    return out


async def search_pjud_via_session(session, competencia: str, form_data: dict, timeout: float) -> dict:
    """Call OJV search via session and parse results."""
    comp_path = competencia_path(competencia)
    html = await asyncio.wait_for(
        session.search(comp_path, form_data),
        timeout=timeout,
    )
    reject_empty_body(html, "search")
    if detect_blocked(html) or len(html.strip()) < 100:
        return {"found": False, "match_count": 0, "matches": [], "blocked": True, "error": None}
    try:
        matches = parse_search_results(html, competencia)
    except ValueError as exc:
        raise UpstreamChangedError("search parser rejected PJUD response") from exc
    if not matches and not is_definitive_not_found(html):
        raise UpstreamChangedError("search response did not contain PJUD results or explicit absence")
    return {
        "found": len(matches) > 0,
        "match_count": len(matches),
        "matches": matches,
        "blocked": False,
        "error": None,
    }


async def detail_pjud_via_session(session, competencia: str, detail_key: str, timeout: float) -> dict:
    """Call OJV detail via session and parse results."""
    comp_path = competencia_path(competencia)
    html = await asyncio.wait_for(
        session.detail(comp_path, detail_key),
        timeout=timeout,
    )
    reject_empty_body(html, "detail")
    if len(html.strip()) < 100 or detect_blocked(html):
        return {"metadata": {}, "movements": [], "litigantes": [], "blocked": True, "error": None}
    parsed = parse_detail(html)
    # Señal de fallo de parseo: la página NO está bloqueada (no bobcmn, >100
    # chars) pero el parser no extrajo NADA — ni metadata, ni movimientos, ni
    # litigantes. Una causa real siempre rinde algo (al menos metadata con el
    # ROL/RIT/Libro), así que "nada" == la página cambió de forma (drift de F5
    # u OJV) o el parser se rompió. No es un bloqueo: no se penaliza ni se
    # re-mintea. Se reporta a ops para intervención humana. Cubre TODAS las
    # competencias (no solo civil) y el hueco de "página rara que no bloquea".
    parse_suspect = not (parsed.get("metadata") or parsed.get("movements") or parsed.get("litigantes"))
    return {**parsed, "blocked": False, "error": None, "parse_suspect": parse_suspect}


class SyncEngine:
    def __init__(
        self, pool, supabase, notifier, metrics, backoff, config: WorkerConfig,
        catalog_service=None, proxy_control=None, proxy_usage=None,
    ):
        self._pool = pool
        self._sb = supabase
        self._notifier = notifier
        self._metrics = metrics
        self._backoff = backoff
        self._config = config
        self._proxy_control = proxy_control or ProxyControl(supabase)
        proxy_url = getattr(config, "OJV_PROXY_URL", None)
        price = getattr(config, "OJV_PROXY_PRICE_PER_GB_USD", DEFAULT_PRICE_PER_GB_USD)
        if not isinstance(price, (int, float)):
            price = DEFAULT_PRICE_PER_GB_USD
        self._proxy_usage = proxy_usage or ProxyUsageTracker(
            supabase,
            enabled=isinstance(proxy_url, str) and bool(proxy_url),
            price_per_gb_usd=float(price),
        )
        self._catalog_service = catalog_service or CatalogService(
            pool, proxy_usage=self._proxy_usage,
        )
        self._last_parse_alert_at = float("-inf")  # cooldown de alertas parse_failed
        self._import_worker = ImportDiscoveryWorker(
            supabase=supabase,
            pool=pool,
            worker_id=getattr(config, "WORKER_ID", "worker-1"),
            fetch_credential=self._get_import_credential,
            concurrency=1,
            enabled=getattr(config, "ENABLE_PJUD_MY_CAUSES_IMPORT", False),
        )
        self._r2 = None
        if config.R2_ENABLED and config.R2_ACCESS_KEY_ID:
            self._r2 = R2Client(
                access_key_id=config.R2_ACCESS_KEY_ID,
                secret_access_key=config.R2_SECRET_ACCESS_KEY,
                endpoint=config.R2_ENDPOINT,
                bucket=config.R2_BUCKET,
            )

    async def process_import_job(self) -> bool:
        """Poll one discovery job outside the paid public batch semaphore."""
        return await self._import_worker.process_next()

    async def _enrich_candidates(
        self, matches: list[dict], request: SearchRequest
    ) -> list[dict]:
        """Enrich parser labels from loaded official data without nested pool I/O."""
        resolved_labels = {}
        enriched: list[dict] = []
        for match in matches:
            label = str(match.get("tribunal", ""))
            if label not in resolved_labels:
                resolved_labels[label] = self._catalog_service.resolve_loaded_tribunal(
                    request.competencia, label, corte=request.corte,
                )
            identity = resolved_labels[label]
            enriched.append({
                **match,
                "tribunal_code": identity.tribunal_code if identity else None,
                "corte_code": identity.court_code if identity else None,
            })
        return enriched

    async def _publish_resolution_candidates(
        self,
        case: dict,
        matches: list[dict],
        total_match_count: int,
    ) -> str:
        """Persist a bounded, key-free candidate generation for user confirmation.

        Search keys are session-bound JWT-like values.  They must never leave the
        worker, so the app receives only the display/identity fields and a stable
        synthetic candidate key.  The case update is fenced by the worker claim;
        if the claim was lost, the generation is discarded and no stale UI state is
        published.
        """
        generation = str(uuid.uuid4())
        now = datetime.now(TZ_SANTIAGO).isoformat()
        bounded_matches = matches[:_MAX_LOOKUP_CANDIDATES]
        candidate_rows = []
        for index, match in enumerate(bounded_matches):
            payload = {
                key: match[key]
                for key in _LOOKUP_CANDIDATE_FIELDS
                if key in match
            }
            payload.update({
                "total_match_count": total_match_count,
                "truncated": total_match_count > len(bounded_matches),
                "generation": generation,
            })
            candidate_rows.append({
                "case_id": case["id"],
                "candidate_key": f"{generation}:{index}",
                "candidate_payload": payload,
            })
        if not candidate_rows:
            raise ValueError("PJUD pidió desambiguar sin entregar candidatos")

        await run_query(self._sb.from_("case_lookup_candidates").insert(candidate_rows))

        external_payload = dict(case.get("external_payload") or {})
        external_payload["lookup_generation"] = generation
        case_update = (
            self._sb.from_("cases")
            .update({
                "tracking_status": "needs_confirmation",
                "last_sync_at": now,
                "last_sync_status": "needs_confirmation",
                "last_sync_error": None,
                "next_sync_at": None,
                "sync_blocked_until": None,
                "external_payload": external_payload,
            })
            .eq("id", case["id"])
            .eq("sync_worker_id", getattr(self._config, "WORKER_ID", "worker-1"))
            .select("id")
            .maybe_single()
        )
        publish_response = await run_query(case_update)
        if getattr(publish_response, "error", None) or not getattr(publish_response, "data", None):
            await run_query(
                self._sb.from_("case_lookup_candidates")
                .update({"discarded_at": now})
                .eq("case_id", case["id"])
                .like("candidate_key", f"{generation}:%")
            )
            raise RuntimeError("No se pudo publicar la generación de candidatos PJUD")

        # Older unselected generations are no longer actionable.  This cleanup is
        # intentionally best-effort: the current generation is already fenced and
        # durable, so a cleanup failure cannot turn a successful publication into a
        # retryable sync error.
        try:
            await run_query(
                self._sb.from_("case_lookup_candidates")
                .update({"discarded_at": now})
                .eq("case_id", case["id"])
                .is_("selected_at", None)
                .is_("discarded_at", None)
                .not_("candidate_key", "like", f"{generation}:%")
            )
        except Exception:
            logger.warning("No se pudieron descartar candidatos anteriores de %s", case["id"], exc_info=True)
        return generation

    async def _handle_transient(self, case: dict, status: str, message: str):
        retry_at = (datetime.now(TZ_SANTIAGO) + timedelta(seconds=_PARSE_RETRY_S)).isoformat()
        await run_query(
            self._sb.from_("cases").update({
                "last_sync_status": status,
                "last_sync_error": message,
                "next_sync_at": retry_at,
            }).eq("id", case["id"])
        )

    async def _begin_sync_run(
        self,
        case: dict,
        run_id: str,
        started_at: datetime,
    ) -> str:
        """Start one claim-bound run, replaying only an ambiguous transport."""
        claim_token = case.get("sync_claim_token")
        worker_id = getattr(self._config, "WORKER_ID", None)
        if not all((
            run_id,
            case.get("id"),
            case.get("law_firm_id"),
            worker_id,
            claim_token,
        )):
            raise RuntimeError("sync_run_identity_unavailable")

        payload = {
            "p_run_id": run_id,
            "p_case_id": case["id"],
            "p_law_firm_id": case["law_firm_id"],
            "p_worker_id": worker_id,
            "p_claim_token": claim_token,
            "p_started_at": started_at.isoformat(),
        }
        attempts = len(_BEGIN_SYNC_RUN_REPLAY_DELAYS_S) + 1
        for attempt in range(attempts):
            try:
                response = await run_query(
                    self._sb.rpc("begin_pjud_scheduled_sync_run", payload)
                )
            except _AMBIGUOUS_BEGIN_SYNC_RUN_ERRORS:
                if attempt + 1 >= attempts:
                    raise
                logger.warning(
                    "Ambiguous scheduled-run start transport; replaying exact RPC"
                )
                await asyncio.sleep(_BEGIN_SYNC_RUN_REPLAY_DELAYS_S[attempt])
                continue

            rows = response.data if isinstance(response.data, list) else []
            if len(rows) != 1:
                raise RuntimeError("sync_run_exact_row_unavailable")
            row = rows[0]
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "status", "error_code"}
                or str(row.get("id")) != run_id
                or row.get("status") != "running"
                or row.get("error_code") is not None
            ):
                raise RuntimeError("sync_run_exact_row_mismatch")
            return run_id

        raise RuntimeError("sync_run_unavailable")

    async def sync_case(self, case: dict) -> dict:
        started_at = datetime.now(TZ_SANTIAGO)

        sync_run_id = str(uuid.uuid4())
        try:
            sync_run_id = await self._begin_sync_run(
                case, sync_run_id, started_at,
            )
        except Exception as exc:
            logger.error(
                "Failed to begin claim-bound sync_run failure_type=%s",
                type(exc).__name__,
            )
            # A durable run is the attribution root for every paid operation.
            # Continuing with ``None`` would collapse all failed inserts onto
            # the same ``None:search`` idempotency key and can associate a
            # later case with an earlier reservation. Stop before acquiring a
            # session; the naturally due case can be claimed again after the
            # persistence boundary recovers.
            self._metrics.record_error("infra")
            self._backoff.record_failure()
            return {
                "success": False,
                "new_movements": 0,
                "status": "sync_run_unavailable",
            }

        session = None
        release_disposition: ReleaseDisposition = "healthy"
        remint_on_release = True
        try:
            # Legacy v1 needs the historical parsed form. Canonical v2 parses
            # inside build_search_form_data so RUC is accepted without forcing
            # it through the old X-NNN-YYYY parser first.
            try:
                parsed = parse_case_identifier(case["case_number"])
            except ValueError:
                parsed = None

            if case.get("matter") == "familia":
                if parsed is None:
                    await self._finish_run(sync_run_id, started_at, "error", 0, "Invalid identifier")
                    await self._update_case_error(case, "Identificador invalido")
                    self._metrics.record_error()
                    return {"success": False, "new_movements": 0}
                result = await self._sync_familia_case(case, sync_run_id, started_at)
                if result["success"]:
                    self._backoff.record_success()
                    self._metrics.record_sync()
                elif result.get("status") != "proxy_billing_exhausted":
                    self._backoff.record_failure()
                    self._metrics.record_error()
                return result

            competencia = MATTER_TO_COMPETENCIA.get(case.get("matter", ""))
            if not competencia:
                await self._finish_run(sync_run_id, started_at, "error", 0, "Unsupported matter")
                await self._update_case_error(case, "Materia no soportada")
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            try:
                canonical_request = _canonical_search_request(case, competencia)
            except InvalidCanonicalIdentityError as exc:
                await self._finish_run(sync_run_id, started_at, "error", 0, "invalid_identity")
                await self._handle_transient(case, "invalid_identity", str(exc))
                self._metrics.record_error("resolution")
                return {"success": False, "new_movements": 0, "status": "invalid_identity"}
            if canonical_request is None and parsed is None:
                await self._finish_run(sync_run_id, started_at, "error", 0, "Invalid identifier")
                await self._update_case_error(case, "Identificador invalido")
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            session = await self._pool.acquire()
            await self._pool.enforce_global_rate_limit()

            if canonical_request is not None:
                form_data = build_search_form_data(
                    competencia=competencia,
                    case_type=canonical_request.case_type,
                    case_number=canonical_request.case_number,
                    corte=canonical_request.corte,
                    tribunal=canonical_request.tribunal,
                    libro=canonical_request.libro,
                    search_mode=canonical_request.search_mode,
                    allow_broad=canonical_request.allow_broad,
                )
            else:
                # For apelaciones, read corte from the case's court_code column
                # (primary) or external_payload.corte (legacy fallback).
                corte_value = ""
                if competencia == "apelaciones":
                    corte_value = str(case.get("court_code") or "")
                    if not corte_value:
                        corte_value = str(
                            case.get("external_payload", {}).get("corte", "") if case.get("external_payload") else ""
                        )
                    if not corte_value:
                        logger.warning(
                            "No court_code for apelaciones case %s; searching all cortes",
                            case.get("case_number", case["id"]),
                        )

                libro_value = case.get("libro") or None
                if not libro_value and case.get("external_payload"):
                    libro_value = case["external_payload"].get("libro") or None

                form_data = build_search_form_data(
                    competencia=competencia,
                    tipo=parsed["tipo"],
                    numero=parsed["numero"],
                    anno=parsed["anno"],
                    corte=corte_value,
                    libro=libro_value,
                )

            # Search
            async with self._proxy_usage.track(
                operation="search",
                law_firm_id=case["law_firm_id"],
                case_id=case["id"],
                sync_run_id=sync_run_id,
                transaction_key=f"{sync_run_id}:search",
            ) as search_usage:
                search_result = await search_pjud_via_session(
                    session, competencia, form_data, self._config.OJV_TIMEOUT_S,
                )
                if search_result["blocked"]:
                    search_usage.status = "blocked"
                    search_usage.error_kind = "ojv"

            if search_result["blocked"]:
                release_disposition = "replace_before_reuse"
                await self._finish_run(sync_run_id, started_at, "blocked", 0, "Blocked by OJV")
                await self._handle_blocked(case["id"], "ojv")
                self._metrics.record_error("infra")
                return {"success": False, "new_movements": 0}

            if not search_result["found"]:
                await self._finish_run(sync_run_id, started_at, "error", 0, "Not found in OJV")
                await self._update_case_error(case, "No encontrada en OJV")
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            # Canonical rows must correlate the fresh same-session search result
            # to one official identifier. Legacy rows preserve v1's historical
            # first-row behavior until their identity is backfilled.
            confirmed_match = None
            if canonical_request is not None:
                candidates = await self._enrich_candidates(search_result["matches"], canonical_request)
                confirmed_match = _confirmed_match(candidates, canonical_request)
                if canonical_request.allow_broad and (
                    confirmed_match is None
                    or confirmed_match.tribunal_code is None
                    or (
                        canonical_request.competencia != "apelaciones"
                        and confirmed_match.corte_code is None
                    )
                ):
                    confirmed_match = None
                if confirmed_match is None:
                    await self._publish_resolution_candidates(
                        case,
                        candidates,
                        search_result.get("match_count", len(candidates)),
                    )
                    await self._finish_run(sync_run_id, started_at, "success", 0)
                    self._backoff.record_success()
                    self._metrics.record_sync()
                    return {
                        "success": True,
                        "new_movements": 0,
                        "status": "needs_confirmation",
                    }

            # Prefer the fresh key from this session: persisted JWTs can expire
            # or belong to a different session and must not contaminate detail.
            detail_key = (
                confirmed_match.key if confirmed_match is not None
                else search_result["matches"][0].get("key") or case.get("external_case_key")
            )
            if not detail_key:
                await self._finish_run(sync_run_id, started_at, "error", 0, "No detail key available")
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            await self._pool.enforce_global_rate_limit()

            # Detail
            async with self._proxy_usage.track(
                operation="detail",
                law_firm_id=case["law_firm_id"],
                case_id=case["id"],
                sync_run_id=sync_run_id,
                transaction_key=f"{sync_run_id}:detail",
            ) as detail_usage:
                detail = await detail_pjud_via_session(
                    session, competencia, detail_key, self._config.OJV_TIMEOUT_S,
                )
                if detail["blocked"]:
                    detail_usage.status = "blocked"
                    detail_usage.error_kind = "ojv"

            if detail["blocked"]:
                release_disposition = "replace_before_reuse"
                await self._finish_run(sync_run_id, started_at, "blocked", 0, "Detail blocked")
                await self._handle_blocked(case["id"], "ojv")
                self._metrics.record_error("infra")
                return {"success": False, "new_movements": 0}

            if detail.get("parse_suspect"):
                # NO seguir al path de éxito: haría upsert vacío y sobrescribiría
                # el external_payload bueno marcando "success". Corto-circuito
                # sin penalizar la causa (no incrementa consecutive_sync_failures).
                # La disposición queda healthy a propósito: el contenido SÍ llegó
                # por esta IP (no es bloqueo ni caída de proxy), el problema es
                # drift de parser/página; re-mintear el slot no ayudaría.
                await self._finish_run(sync_run_id, started_at, "error", 0, "parse_failed")
                await self._handle_parse_suspect(case, competencia)
                self._metrics.record_error("infra")
                return {"success": False, "new_movements": 0}

            identity_update = None
            if (
                canonical_request is not None
                and canonical_request.allow_broad
                and confirmed_match is not None
            ):
                # This happens after the same-session detail is known good but
                # before *any* movement, document, payload or success write.
                # A concurrent human selection wins over this automatic guess.
                identity_update = {
                    "tribunal_code": confirmed_match.tribunal_code,
                    "tribunal_unknown": False,
                    "court": confirmed_match.tribunal,
                }
                if canonical_request.competencia != "apelaciones":
                    identity_update["court_code"] = confirmed_match.corte_code

                identity_query = (
                    self._sb.from_("cases").update(identity_update)
                    .eq("id", case["id"])
                    .eq("tribunal_unknown", True)
                    .is_("tribunal_code", "null")
                )
                if canonical_request.competencia == "apelaciones":
                    identity_query = identity_query.eq("court_code", case.get("court_code"))
                else:
                    identity_query = identity_query.is_("court_code", "null")
                # postgrest's filtered update builder already returns its
                # representation.  Chaining select() here is unsupported in
                # the installed client and would turn every broad sync into an
                # AttributeError before the CAS result can be inspected.
                identity_result = await run_query(identity_query)
                if not getattr(identity_result, "data", None):
                    await self._finish_run(
                        sync_run_id, started_at, "error", 0, "identity_changed",
                    )
                    self._metrics.record_error("resolution")
                    return {
                        "success": False,
                        "new_movements": 0,
                        "status": "identity_changed",
                    }

            # Upsert movements
            new_count = await self._upsert_movements(case, detail)

            # Update case
            latest_date = _get_latest_movement_date(detail["movements"])

            canonical = (
                f"{competencia}:{canonical_request.case_type}:{canonical_request.case_number}"
                if canonical_request is not None
                else f"{competencia}:{parsed['tipo']}:{parsed['numero']}:{parsed['anno']}"
            )

            case_update = {
                    "tracking_status": "active",
                    "last_sync_at": datetime.now(TZ_SANTIAGO).isoformat(),
                    "last_sync_status": "success",
                    "last_sync_error": None,
                    # Se RESETEA en el éxito. Incrementarlo acá suspendía causas
                    # sanas tras 10 sincronizaciones buenas (el bug que motivó el
                    # rename de esta columna).
                    "consecutive_sync_failures": 0,
                    "canonical_identifier": canonical,
                    "external_case_key": case.get("external_case_key") or detail_key,
                    "external_payload": sanitize_pjud_case_external_payload({
                        "metadata": detail["metadata"],
                        "litigantes": detail["litigantes"],
                    }),
            }
            await run_query(self._sb.from_("cases").update(case_update).eq("id", case["id"]))
            # PostgreSQL lee is_urgent bajo row lock: un toggle concurrente no
            # puede ser revertido por el snapshot viejo reclamado al inicio.
            await run_query(self._sb.rpc("schedule_pjud_case_after_sync", {
                "p_case_id": case["id"],
                "p_latest_movement_date": latest_date,
            }))

            # Finish sync run
            await self._finish_run(sync_run_id, started_at, "success", new_count)

            # Notify if new movements
            if new_count > 0:
                await self._notifier.notify_new_movements(case, new_count)

            self._backoff.record_success()
            self._metrics.record_sync()
            logger.info("Synced case %s: %d new movements", case["case_number"], new_count)
            return {"success": True, "new_movements": new_count}

        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            release_disposition = "replace_before_reuse"
            await self._finish_run(sync_run_id, started_at, "error", 0, "pjud_timeout")
            await self._handle_transient(case, "pjud_timeout", str(e))
            self._metrics.record_error("infra")
            return {"success": False, "new_movements": 0, "status": "pjud_timeout"}
        except UpstreamChangedError as e:
            await self._finish_run(sync_run_id, started_at, "error", 0, "upstream_changed")
            await self._handle_transient(case, "upstream_changed", str(e))
            self._metrics.record_error("infra")
            return {"success": False, "new_movements": 0, "status": "upstream_changed"}
        except (ProxyBudgetExceededError, ProxyUsagePersistenceError) as e:
            await self._finish_run(
                sync_run_id, started_at, "blocked", 0, "infra_unavailable",
            )
            if isinstance(e, ProxyUsagePersistenceError):
                await self._proxy_control.pause_telemetry_unavailable()
                self._backoff.open_permanently("proxy_cost_control")
            elif e.blocking_scope == "global":
                await self._proxy_control.refresh()
                self._backoff.open_permanently("proxy_cost_control")
            await self._update_case_blocked(case["id"], "infra", None)
            self._metrics.record_error("infra")
            globally_paused = (
                isinstance(e, ProxyUsagePersistenceError)
                or e.blocking_scope == "global"
            )
            await send_ops_alert(
                getattr(self._config, "TELEGRAM_BOT_TOKEN", ""),
                getattr(self._config, "TELEGRAM_CHAT_ID", ""),
                "proxy_cost_control_paused" if globally_paused else "proxy_budget_blocked",
                (
                    "Trafico PJUD detenido por presupuesto o telemetria; requiere revision en ops."
                    if globally_paused
                    else f"Sincronizacion PJUD bloqueada por presupuesto {e.blocking_scope}."
                ),
            )
            return {
                "success": False,
                "new_movements": 0,
                "status": (
                    "proxy_cost_control_paused"
                    if globally_paused
                    else "proxy_budget_blocked"
                ),
            }
        except Exception as e:
            # Un solo `except`, clasificado, en vez de dos listas de tipos. La
            # anterior era `(httpx.TransportError, asyncio.TimeoutError)`, y
            # `httpx.HTTPStatusError` NO es `TransportError` —son hermanos bajo
            # `HTTPError`—, así que un 503 de OJV se caía al `except Exception`
            # de abajo: `_update_case_error`, contador++, y a las 10 la causa
            # `suspended` por una tarde en que el portal de ellos estuvo caído.
            # La app lo clasificaba bien desde la PR #55 (`pjudHttpError`); este
            # lado no, y los dos escriben sobre las mismas filas.
            if is_proxy_billing_error(e):
                release_disposition = "replace_before_reuse"
                remint_on_release = False
                await self._finish_run(
                    sync_run_id, started_at, "blocked", 0, "infra_unavailable",
                )
                await self._proxy_control.trip_billing_exhausted()
                self._backoff.open_permanently("billing_exhausted")
                # Tenant-facing state stays generic. The provider status and
                # raw transport detail are restricted to ops telemetry/logs.
                await self._update_case_blocked(case["id"], "infra", None)
                self._metrics.record_error("infra")
                await send_ops_alert(
                    getattr(self._config, "TELEGRAM_BOT_TOKEN", ""),
                    getattr(self._config, "TELEGRAM_CHAT_ID", ""),
                    "proxy_billing_exhausted",
                    "Proxy residencial detenido por facturacion; requiere reactivacion explicita en ops.",
                )
                return {
                    "success": False,
                    "new_movements": 0,
                    "status": "proxy_billing_exhausted",
                }

            kind = classify_exception(e)

            if kind == "case":
                msg = str(e)
                logger.exception("Error syncing case %s", case["case_number"])
                await self._finish_run(
                    sync_run_id,
                    started_at,
                    "error",
                    0,
                    msg,
                    error_code=_sync_run_error_code(e, failure_kind=kind),
                )
                await self._update_case_error(case, msg)
                self._backoff.record_failure()
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            # infra u ojv: transitorio. Se trata como bloqueo —re-mint del slot
            # (`replace_before_reuse`) SIN penalizar: sin `_update_case_error`,
            # sin `consecutive_sync_failures++`—. El circuit breaker global vía
            # _handle_blocked/record_blocked da la protección sistémica.
            #
            # La excepción es la falta de token CSRF, donde re-mintear no puede
            # corregir nada y sale carísimo: ver `slot_still_healthy`.
            mapped_disposition = _release_disposition_for_error(
                e,
                transport_revalidation_enabled=(
                    getattr(
                        self._config,
                        "WORKER_TRANSPORT_REVALIDATION_ENABLED",
                        False,
                    )
                    is True
                ),
            )
            release_disposition = mapped_disposition
            msg = f"{kind}: {type(e).__name__}: {e}"
            if release_disposition == "healthy":
                # Tiene que hacer ruido. Sin el re-mint esto falla barato, y
                # barato + `logger.warning` = invisible hasta que alguien mire.
                # El watchdog cuenta tracebacks (3 en una hora dispara alerta),
                # así que `exception` es la señal que ya existe.
                logger.exception(
                    "Fallo %s sincronizando causa %s SIN re-mint del slot: %s",
                    kind, case["case_number"], msg,
                )
            else:
                logger.warning("Fallo %s sincronizando causa %s: %s", kind, case["case_number"], msg)
            await self._finish_run(
                sync_run_id,
                started_at,
                "blocked",
                0,
                msg,
                error_code=_sync_run_error_code(e, failure_kind=kind),
            )
            await self._handle_blocked(case["id"], kind, msg)
            # "infra" fijo aunque `kind` pueda ser "ojv": el heartbeat del worker
            # tiene dos baldes, `errors_infra_today` y `errors_case_today`, y la
            # pregunta que responde es "¿esta falla es culpa de la causa?". Un
            # bloqueo de OJV no lo es. Mandarlo al balde de causa por ser un
            # tercer valor seria contarlo como lo contrario de lo que es.
            self._metrics.record_error("infra")
            return {"success": False, "new_movements": 0}

        finally:
            if session:
                if remint_on_release:
                    await self._pool.release(
                        session, disposition=release_disposition,
                    )
                else:
                    await self._pool.release(
                        session,
                        disposition=release_disposition,
                        remint=False,
                    )

    async def _call_app_internal(
        self, method: str, path: str, what: str, *, law_firm_id: str | None = None
    ) -> httpx.Response | None:
        """Una request a la API interna de la app, o `None` si no salio.

        La configuracion y el bearer son propiedad del cliente, no de cada
        endpoint: esta es la superficie por donde viaja todo hecho de credencial
        entre los dos servicios, y cuando eran dos copias ya trataban distinto un
        no-200. `what` es solo para el log.
        """
        url = self._config.VERCEL_APP_URL
        key = self._config.INTERNAL_CREDENTIALS_API_KEY
        if not url or not key:
            logger.error("VERCEL_APP_URL or INTERNAL_CREDENTIALS_API_KEY not configured")
            return None
        try:
            headers = {"Authorization": f"Bearer {key}"}
            if law_firm_id is not None:
                headers["X-Law-Firm-Id"] = law_firm_id
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(
                    method, f"{url}{path}", headers=headers
                )
            if resp.status_code != 200:
                logger.warning("%s returned %d (%s)", what, resp.status_code, path)
            return resp
        except Exception:
            logger.exception("%s failed (%s)", what, path)
            return None

    async def _get_decrypted_credential(
        self, credential_id: str, law_firm_id: str
    ) -> dict | None:
        """Fetch decrypted credential from Vercel internal endpoint."""
        resp = await self._call_app_internal(
            "GET",
            f"/api/internal/credentials/{credential_id}/decrypt",
            "Decrypt endpoint",
            law_firm_id=law_firm_id,
        )
        return resp.json() if resp is not None and resp.status_code == 200 else None

    async def _get_import_credential(
        self, credential_id: str, law_firm_id: str,
    ) -> dict | None:
        """Keep terminal credential absence distinct from internal outages."""
        resp = await self._call_app_internal(
            "GET",
            f"/api/internal/credentials/{credential_id}/decrypt",
            "Import decrypt endpoint",
            law_firm_id=law_firm_id,
        )
        if resp is None or resp.status_code in {401, 403, 408, 429} or resp.status_code >= 500:
            raise ImportCredentialInfrastructureError("import_credential_boundary_unavailable")
        if resp.status_code in {404, 410, 422}:
            return None
        if resp.status_code != 200:
            raise ImportCredentialInfrastructureError("import_credential_boundary_unexpected")
        data = resp.json()
        if not isinstance(data, dict) or any(
            not isinstance(data.get(field), str) or not data[field]
            for field in ("rut", "password", "password_type")
        ) or data["password_type"] != "clave_poder_judicial":
            raise ImportCredentialInfrastructureError("import_credential_contract_invalid")
        return data

    async def _report_invalid_credential(self, credential_id: str) -> None:
        """Tell the app that OJV rejected this credential.

        El worker es el que ve el veredicto y el unico que puede reportarlo: la
        app no reintenta la causa (queda `suspended`, que es terminal), asi que
        su propio camino no vuelve a pasar por aca nunca. Sin este aviso la
        credencial se quedaba con badge verde "Activa" mientras N causas
        quedaban suspendidas sin motivo visible.

        No devuelve nada y no cambia el destino de la causa: la causa ya se
        marco terminal antes de llamar. Un fallo de red aca no puede convertir
        un veredicto en un reintento — por eso `_call_app_internal` se traga sus
        propias excepciones.
        """
        await self._call_app_internal(
            "POST",
            f"/api/internal/credentials/{credential_id}/invalidate",
            "Invalidate endpoint",
        )

    async def _sync_familia_case(self, case: dict, sync_run_id: str | None, started_at: datetime) -> dict:
        credential_id = case.get("ojv_credential_id")
        if not credential_id:
            await self._finish_run(sync_run_id, started_at, "error", 0, "Missing ojv_credential_id")
            await self._terminal_error(case["id"], "Causa Familia sin credencial OJV configurada")
            return {"success": False, "new_movements": 0}

        cred = await self._get_decrypted_credential(
            credential_id, case["law_firm_id"]
        )
        if not cred:
            await self._finish_run(sync_run_id, started_at, "error", 0, "Credential inactive or missing")
            await self._terminal_error(case["id"], "Credencial OJV inactiva o no encontrada")
            return {"success": False, "new_movements": 0}

        try:
            parsed = parse_case_identifier(case["case_number"])
        except ValueError:
            await self._finish_run(sync_run_id, started_at, "error", 0, "Invalid identifier")
            await self._terminal_error(case["id"], "Identificador invalido")
            return {"success": False, "new_movements": 0}

        # Solo Clave PJ (Clave Única quedó dormida). Cualquier otro password_type
        # es terminal — no crashea ni penaliza en loop (Gap #5).
        if cred.get("password_type") != "clave_poder_judicial":
            await self._finish_run(sync_run_id, started_at, "error", 0, "auth_type no soportado")
            await self._terminal_error(case["id"], "Método de credencial no soportado — reingresá con Clave Poder Judicial")
            return {"success": False, "new_movements": 0}
        auth_type = "clave_pj"

        # Bundle F5 del pool, FUERA del timeout de 90s: el minteo no es culpa de
        # la causa (Gap #6). El slot queda busy hasta release_familia_bundle.
        bundle, slot = await self._pool.acquire_familia_bundle()
        if bundle is None:
            await self._pool.release_familia_bundle(
                slot, disposition="healthy",
            )
            await self._finish_run(sync_run_id, started_at, "blocked", 0, "Pool sin bundle F5")
            await self._handle_blocked(case["id"], "infra", "Pool sin bundle F5")
            self._metrics.record_error("infra")
            return {"success": False, "new_movements": 0}

        release_disposition: ReleaseDisposition = "healthy"
        remint_on_release = True
        try:
            try:
                async with self._proxy_usage.track(
                    operation="search",
                    law_firm_id=case["law_firm_id"],
                    case_id=case["id"],
                    sync_run_id=sync_run_id,
                    transaction_key=f"{sync_run_id}:familia-search",
                ):
                    async with asyncio.timeout(90):
                        async with FamiliaAuthSession(
                            bundle.proxy_url, bundle.cookies, bundle.user_agent, rate_limit_s=2.5,
                        ) as session:
                            try:
                                await session.login(
                                    SecretStr(cred["rut"]),
                                    SecretStr(cred["password"]),
                                    auth_type,
                                )
                            except InvalidCredentialsError:
                                # Credencial inválida = terminal (no reintenta en loop);
                                # la IP está sana, se libera healthy=True.
                                await self._finish_run(sync_run_id, started_at, "error", 0, "Invalid credentials")
                                await self._terminal_error(case["id"], "Credencial OJV invalida — verifica en Configuracion")
                                # El veredicto va TAMBIEN a la credencial, no solo a
                                # la causa: es lo que el abogado tiene que arreglar,
                                # y la app no tiene otra forma de enterarse.
                                await self._report_invalid_credential(credential_id)
                                return {"success": False, "new_movements": 0}

                            html = await session.search_familia(
                                rut=SecretStr(cred["rut"]),
                                rit=str(parsed["numero"]),
                                year=str(parsed["anno"]),
                            )
            # Bloqueo F5 / sesión / timeout / transporte = transitorio: NO penaliza
            # consecutive_sync_failures (_handle_blocked), y marca el slot para re-mint
            # (healthy=False en el finally). str(e) preserva el detalle (los
            # FamiliaBlockedError ya dicen "login"/"search"); TimeoutError es vacío.
            # `httpx.HTTPStatusError` en la lista y no solo `TransportError`:
            # son hermanos, no padre e hijo, asi que un 503 de OJV durante el
            # login de Familia se caia al `except Exception` de mas afuera y
            # terminaba penalizando la causa.
            except (
                FamiliaBlockedError,
                OjvSessionError,
                ProxyBillingExhaustedError,
                TimeoutError,
                httpx.TransportError,
                httpx.HTTPStatusError,
            ) as e:
                mapped_disposition = _release_disposition_for_error(
                    e,
                    transport_revalidation_enabled=(
                        getattr(
                            self._config,
                            "WORKER_TRANSPORT_REVALIDATION_ENABLED",
                            False,
                        )
                        is True
                    ),
                )
                release_disposition = mapped_disposition
                if is_proxy_billing_error(e):
                    remint_on_release = False
                    await self._finish_run(
                        sync_run_id, started_at, "blocked", 0, "infra_unavailable",
                    )
                    await self._proxy_control.trip_billing_exhausted()
                    self._backoff.open_permanently("billing_exhausted")
                    await self._update_case_blocked(case["id"], "infra", None)
                    self._metrics.record_error("infra")
                    await send_ops_alert(
                        getattr(self._config, "TELEGRAM_BOT_TOKEN", ""),
                        getattr(self._config, "TELEGRAM_CHAT_ID", ""),
                        "proxy_billing_exhausted",
                        "Proxy residencial detenido por facturacion; requiere reactivacion explicita en ops.",
                    )
                    return {
                        "success": False,
                        "new_movements": 0,
                        "status": "proxy_billing_exhausted",
                    }
                msg = str(e) or "Timeout Familia sync"
                # El unico `except` de los call sites que mezcla las dos causas:
                # `FamiliaBlockedError` ES el portal cortandonos, y las otras
                # (sesion que no levanta, timeout, transporte) son nuestras.
                # Agruparlas para el retry esta bien —todas son transitorias y
                # ninguna penaliza a la causa—; agruparlas para el mensaje seria
                # volver a inventar la culpa.
                #
                # `FamiliaBlockedError` va explicito y no dentro de
                # `block_cause`: es una excepcion de dominio de Familia, y meter
                # `app.familia.auth` dentro del clasificador le arrastraria ese
                # grafo entero a las rutas de search y detail, que no lo usan.
                cause = "ojv" if isinstance(e, FamiliaBlockedError) else block_cause(e)
                await self._finish_run(
                    sync_run_id,
                    started_at,
                    "blocked",
                    0,
                    msg,
                    error_code=_sync_run_error_code(e, failure_kind=cause),
                )
                await self._handle_blocked(case["id"], cause, msg)
                self._metrics.record_error("infra")
                return {"success": False, "new_movements": 0}
        finally:
            if remint_on_release:
                await self._pool.release_familia_bundle(
                    slot, disposition=release_disposition,
                )
            else:
                await self._pool.release_familia_bundle(
                    slot, disposition=release_disposition, remint=False,
                )

        casos, err = parse_familia_results(html)
        if err and err != "no_cases":
            await self._finish_run(
                sync_run_id,
                started_at,
                "error",
                0,
                f"Parse error: {err}",
                error_code="parse_failed",
            )
            await self._update_case_error(case, "Error al interpretar respuesta OJV Familia")
            return {"success": False, "new_movements": 0}

        if not casos:
            await self._finish_run(sync_run_id, started_at, "error", 0, "Case not found in Familia portal")
            await self._update_case_error(case, "Causa no encontrada en portal Familia")
            return {"success": False, "new_movements": 0}

        caso = casos[0]

        prev_payload = case.get("external_payload") or {}
        prev_estado = prev_payload.get("estado")
        estado_changed = prev_estado != caso.estado

        new_count = 0
        if estado_changed or not prev_estado:
            mov_title = (
                f"Estado actualizado: {prev_estado} → {caso.estado}"
                if prev_estado
                else f"Estado: {caso.estado}"
            )
            ext_key = f"familia:{case['case_number']}:{caso.estado}"
            try:
                await run_query(
                    self._sb.from_("case_movements").upsert(
                        {
                            "law_firm_id": case["law_firm_id"],
                            "case_id": case["id"],
                            "date": datetime.now(TZ_SANTIAGO).date().isoformat(),
                            "title": mov_title,
                            "description": f"Tribunal: {caso.tribunal} | Materia: {caso.materia}",
                            "movement_type": "resolution",
                            "source": "sync",
                            "include_in_report": True,
                            "external_movement_key": ext_key,
                            "raw_payload": caso.model_dump(),
                        },
                        on_conflict="case_id,external_movement_key",
                        ignore_duplicates=False,
                    )
                )
                # Only notify on a genuine estado change, not the initial registration
                if estado_changed and prev_estado:
                    new_count = 1
            except Exception:
                logger.warning("Failed to upsert familia movement for case %s", case["id"], exc_info=True)

        await run_query(
            self._sb.from_("cases").update({
                "tracking_status": "active",
                "last_sync_at": datetime.now(TZ_SANTIAGO).isoformat(),
                "last_sync_status": "success",
                "last_sync_error": None,
                # Se resetea en el éxito, igual que el path PJUD de sync_case.
                "consecutive_sync_failures": 0,
                "sync_blocked_until": None,
                "court": caso.tribunal or case.get("court", ""),
                "title": caso.caratulado or case.get("title", ""),
                "external_payload": {
                    "estado": caso.estado,
                    "materia": caso.materia,
                    "tribunal": caso.tribunal,
                    "caratulado": caso.caratulado,
                    "fecha_ingreso": caso.fecha_ingreso,
                },
            }).eq("id", case["id"])
        )
        await run_query(self._sb.rpc("schedule_pjud_case_after_sync", {
            "p_case_id": case["id"],
            # Familia no trae movimientos públicos en este flujo. Preservar la
            # fecha importada/existente evita borrarla y permite clasificar una
            # causa realmente inactiva como semanal.
            "p_latest_movement_date": case.get("latest_movement_date"),
        }))

        await self._finish_run(sync_run_id, started_at, "success", new_count)

        if new_count > 0:
            try:
                await self._notifier.notify_new_movements(case, new_count)
            except Exception:
                logger.warning("Failed to notify for familia case %s", case["id"], exc_info=True)

        logger.info("Familia sync OK for case %s — estado: %s", case["case_number"], caso.estado)
        return {"success": True, "new_movements": new_count}

    async def _terminal_error(self, case_id: str, error: str):
        """Keep terminal failures alertable; ``paused`` is reserved for user pauses."""
        await run_query(
            self._sb.from_("cases").update({
                "tracking_status": "suspended",
                "last_sync_status": "error",
                "last_sync_error": error,
            }).eq("id", case_id)
        )

    async def _upsert_movements(self, case: dict, detail: dict) -> int:
        movements = detail.get("movements", [])
        if not movements:
            return 0

        preliminary = _prepare_pjud_movements(case, movements, log_undated=True)
        if not preliminary:
            return 0
        existing_movements = await self._load_existing_movements(case["id"])
        prepared = _prepare_pjud_movements(
            case,
            movements,
            log_undated=False,
            existing_movements=existing_movements,
        )

        rows = []
        sources_by_external_key: dict[str, list[dict]] = {}
        for mov, external_key in prepared:
            sources = extract_pjud_document_sources(mov, external_key)
            sources_by_external_key[external_key] = sources
            rows.append({
                "law_firm_id": case["law_firm_id"],
                "case_id": case["id"],
                "date": mov.get("fecha"),
                "title": f"{mov.get('tramite', '')}: {mov.get('descripcion', '')}",
                "description": f"Cuaderno: {mov.get('cuaderno', '')} | Folio: {mov.get('folio', '')} | Etapa: {mov.get('etapa', '')}",
                "movement_type": _map_tramite(mov.get("tramite", "")),
                "source": "sync",
                # Provider URLs and JWTs are ephemeral credentials. Only the
                # service-only registry receives stable availability metadata.
                "document_url": None,
                "has_remote_document": bool(sources),
                "is_relevant": True,
                "include_in_report": True,
                "external_movement_key": external_key,
                "raw_payload": sanitize_pjud_movement_payload(mov),
            })

        base_keys = [
            _build_movement_external_key(case["case_number"], movement)
            for movement, _ in prepared
        ]
        n_dupes = sum(
            1
            for row, base_key in zip(rows, base_keys)
            if row["external_movement_key"] != base_key
        )
        if n_dupes:
            logger.warning(
                "Causa %s: %d movimiento(s) con cuaderno+folio repetido en el mismo "
                "batch; se les agrega sufijo para no perderlos",
                case["case_number"], n_dupes,
            )

        # Count before
        before_resp = await run_query(
            self._sb.from_("case_movements")
            .select("id", count="exact")
            .eq("case_id", case["id"])
        )
        before_count = before_resp.count if before_resp.count is not None else 0

        # Upsert (update on conflict)
        upsert_response = await run_query(
            self._sb.from_("case_movements").upsert(
                rows,
                on_conflict="case_id,external_movement_key",
                ignore_duplicates=False,
            )
        )

        # Count after
        after_resp = await run_query(
            self._sb.from_("case_movements")
            .select("id", count="exact")
            .eq("case_id", case["id"])
        )
        after_count = after_resp.count if after_resp.count is not None else 0

        upserted_rows = (
            upsert_response.data
            if isinstance(getattr(upsert_response, "data", None), list)
            else []
        )
        await self._persist_document_sources(
            case,
            upserted_rows,
            sources_by_external_key,
        )

        return after_count - before_count

    async def _persist_document_sources(
        self,
        case: dict,
        upserted_rows: list[dict],
        sources_by_external_key: dict[str, list[dict]],
    ) -> None:
        """Best-effort service-only source registry update.

        Expand deployments may briefly run before migration 00069 exists. A
        missing registry must never turn a valid movement sync into a failure.
        """
        entries = []
        for row in upserted_rows:
            movement_id = row.get("id")
            external_key = row.get("external_movement_key")
            if not movement_id or external_key not in sources_by_external_key:
                continue
            entries.append({
                "movement_id": movement_id,
                "sources": sources_by_external_key[external_key],
            })
        if not entries:
            return

        try:
            await asyncio.wait_for(
                run_query(self._sb.rpc("upsert_pjud_document_source_batch", {
                    "p_law_firm_id": case["law_firm_id"],
                    "p_case_id": case["id"],
                    "p_entries": entries,
                })),
                timeout=_DOCUMENT_SOURCE_TIMEOUT_S,
            )
        except Exception:
            logger.warning(
                "PJUD document source registry update skipped",
                extra={
                    "case_id": case.get("id"),
                    "movement_count": len(entries),
                    "source_count": sum(len(entry["sources"]) for entry in entries),
                },
            )

    async def _download_and_store_documents(
        self, case: dict, detail: dict, session, sync_run_id: str | None = None,
    ) -> None:
        """Download PJUD documents and upload to R2.

        Builds a per-movement ``documents`` JSONB array and writes it
        atomically alongside ``document_storage_key`` so the two columns
        never go out of sync.
        """
        movements = detail.get("movements", [])
        if not movements:
            return

        preliminary = _prepare_pjud_movements(case, movements, log_undated=False)
        if not preliminary:
            return
        existing_movements = await self._load_existing_movements(case["id"])
        prepared = _prepare_pjud_movements(
            case,
            movements,
            log_undated=False,
            existing_movements=existing_movements,
        )
        movements = [movement for movement, _ in prepared]
        movement_keys = [external_key for _, external_key in prepared]
        existing_by_key = {
            row.get("external_movement_key"): row
            for row in existing_movements
            if row.get("external_movement_key")
        }
        movement_ids = {
            key: row.get("id") for key, row in existing_by_key.items()
        }

        # Begin with the durable history. A partial PJUD response or a transient
        # anexo failure must never erase document references already stored.
        per_movement_docs: dict[str, list[dict]] = {
            key: [dict(doc) for doc in (row.get("documents") or [])]
            for key, row in existing_by_key.items()
            if key in movement_keys and row.get("documents")
        }

        def remember_document(ext_key: str, entry: dict) -> None:
            stored = per_movement_docs.setdefault(ext_key, [])
            replacement = None
            for index, current in enumerate(stored):
                if entry.get("type") == "principal" and current.get("type") == "principal":
                    replacement = index
                    break
                if entry.get("storage_key") == current.get("storage_key"):
                    replacement = index
                    break
                if (
                    entry.get("source_id")
                    and entry.get("source_id") == current.get("source_id")
                    and entry.get("type") == current.get("type")
                ):
                    replacement = index
                    break
            if replacement is None:
                stored.append(entry)
            else:
                stored[replacement] = entry

        async def existing_document(
            ext_key: str,
            doc_type: str,
            source_id: str | None = None,
            legacy_index: int | None = None,
        ) -> dict | None:
            stored = existing_by_key.get(ext_key, {}).get("documents") or []
            candidates = [doc for doc in stored if doc.get("type") == doc_type]
            match = None
            if source_id is not None:
                match = next(
                    (doc for doc in candidates if doc.get("source_id") == source_id),
                    None,
                )
            legacy_candidates = [doc for doc in candidates if not doc.get("source_id")]
            if (
                match is None
                and legacy_index is not None
                and legacy_index < len(legacy_candidates)
            ):
                # Backward-compatible bridge for rows written before source_id.
                match = legacy_candidates[legacy_index]
            if not match or not match.get("storage_key"):
                return None
            if not await self._r2.exists(match["storage_key"]):
                return None
            return {**match, **({"source_id": source_id} if source_id else {})}

        try:
            pending_primary_movements: list[dict] = []
            pending_primary_keys: list[str] = []
            for mov, ext_key in zip(movements, movement_keys):
                if not (mov.get("documento_url") and mov.get("documento_token")):
                    continue
                stored_primary = await existing_document(ext_key, "principal", legacy_index=0)
                if stored_primary:
                    remember_document(ext_key, stored_primary)
                    async with self._proxy_usage.track(
                        operation="document_primary",
                        law_firm_id=case["law_firm_id"],
                        case_id=case["id"],
                        sync_run_id=sync_run_id,
                        movement_id=movement_ids.get(ext_key),
                        transaction_key=f"{sync_run_id}:primary:{ext_key}:skip",
                        estimated_bytes=0,
                    ) as usage:
                        usage.documents_skipped += 1
                else:
                    pending_primary_movements.append(mov)
                    pending_primary_keys.append(ext_key)

            def primary_usage_scope(index: int):
                ext_key = pending_primary_keys[index]
                return self._proxy_usage.track(
                    operation="document_primary",
                    law_firm_id=case["law_firm_id"],
                    case_id=case["id"],
                    sync_run_id=sync_run_id,
                    movement_id=movement_ids.get(ext_key),
                    transaction_key=f"{sync_run_id}:primary:{ext_key}",
                )

            docs = await download_documents(
                session, pending_primary_movements, primary_usage_scope,
            )

            # --- Primary documents ---
            for doc in docs:
                mov = pending_primary_movements[doc.index]
                ext_key = pending_primary_keys[doc.index]
                r2_key = f"{case['law_firm_id']}/{case['id']}/{ext_key}.{doc.extension}"

                if await self._r2.exists(r2_key):
                    # Already in R2 — still track it so the documents array is complete
                    remember_document(ext_key, {
                        "type": "principal",
                        "storage_key": r2_key,
                        "content_type": doc.content_type,
                        "label": "Documento",
                    })
                    continue

                try:
                    await self._r2.upload(r2_key, doc.data, doc.content_type)
                    remember_document(ext_key, {
                        "type": "principal",
                        "storage_key": r2_key,
                        "content_type": doc.content_type,
                        "label": "Documento",
                    })
                except Exception as exc:
                    if is_proxy_billing_error(exc) or is_proxy_cost_control_error(exc):
                        raise
                    logger.warning("Failed to upload document %s", r2_key, exc_info=True)

            logger.info(
                "Stored %d new primary documents for case %s (%d already present)",
                len(docs),
                case["case_number"],
                len(per_movement_docs) - len(docs),
            )

            # --- Certificados (iterate ALL movements, not just downloaded docs) ---
            for mov, ext_key in zip(movements, movement_keys):
                extras = mov.get("documentos_adicionales", [])
                if not extras:
                    continue

                for cert_i, cert in enumerate(extras):
                    cert_url = cert.get("url", "")
                    cert_token = cert.get("token", "")
                    cert_param = cert.get("param", "dtaCert")
                    if not cert_url or not cert_token:
                        continue

                    source_id = _document_source_id(
                        "certificate", cert_url, cert_param, cert_i,
                    )
                    stored_cert = await existing_document(
                        ext_key, "certificado", source_id, cert_i,
                    )
                    if stored_cert:
                        remember_document(ext_key, stored_cert)
                        async with self._proxy_usage.track(
                            operation="certificate",
                            law_firm_id=case["law_firm_id"],
                            case_id=case["id"],
                            sync_run_id=sync_run_id,
                            movement_id=movement_ids.get(ext_key),
                            transaction_key=f"{sync_run_id}:certificate:{ext_key}:{source_id}:skip",
                            estimated_bytes=0,
                        ) as usage:
                            usage.documents_skipped += 1
                        continue

                    try:
                        async with self._proxy_usage.track(
                            operation="certificate",
                            law_firm_id=case["law_firm_id"],
                            case_id=case["id"],
                            sync_run_id=sync_run_id,
                            movement_id=movement_ids.get(ext_key),
                            transaction_key=f"{sync_run_id}:certificate:{ext_key}:{source_id}",
                        ) as usage:
                            cert_doc = await download_single_document(
                                session, cert_url, cert_token, cert_param,
                            )
                            if cert_doc:
                                usage.documents_downloaded += 1
                        if not cert_doc:
                            continue

                        suffix = f"-cert" if len(extras) == 1 else f"-cert-{cert_i}"
                        r2_key = f"{case['law_firm_id']}/{case['id']}/{ext_key}{suffix}.{cert_doc.extension}"

                        await self._r2.upload(r2_key, cert_doc.data, cert_doc.content_type)
                        remember_document(ext_key, {
                            "type": "certificado",
                            "source_id": source_id,
                            "storage_key": r2_key,
                            "content_type": cert_doc.content_type,
                            "label": "Certificado",
                        })
                        logger.info("Uploaded cert %s (%d bytes)", r2_key, len(cert_doc.data))
                    except Exception as exc:
                        if is_proxy_billing_error(exc) or is_proxy_cost_control_error(exc):
                            raise
                        logger.warning("Failed to download/upload cert for %s", ext_key, exc_info=True)
                        # Cert failure must NOT block primary or anexos

            # --- Anexos (iterate ALL movements, not just downloaded docs) ---
            for mov, ext_key in zip(movements, movement_keys):
                anexo_func = mov.get("anexo_func")
                anexo_token = mov.get("anexo_token")
                if not anexo_func or not anexo_token:
                    continue

                endpoint_info = ANEXO_ENDPOINTS.get(anexo_func)
                if not endpoint_info:
                    logger.warning("Unknown anexo function '%s' for folio %s — skipping", anexo_func, mov.get("folio"))
                    continue

                endpoint, param = endpoint_info
                try:
                    async with self._proxy_usage.track(
                        operation="anexo_list",
                        law_firm_id=case["law_firm_id"],
                        case_id=case["id"],
                        sync_run_id=sync_run_id,
                        movement_id=movement_ids.get(ext_key),
                        transaction_key=f"{sync_run_id}:anexo-list:{ext_key}",
                    ):
                        anexo_html = await session.fetch_anexo_list(
                            endpoint, param, anexo_token,
                        )
                    anexo_files = parse_anexo_list(anexo_html)

                    if not anexo_files:
                        logger.info("No anexo files found for folio %s", mov.get("folio"))
                        continue

                    for anexo_i, anexo_file in enumerate(anexo_files):
                        try:
                            source_id = _document_source_id(
                                "anexo_document",
                                anexo_file.get("download_url"),
                                anexo_file.get("download_param", "dtaDoc"),
                                anexo_file.get("codigo"),
                                anexo_file.get("label", "Anexo"),
                            )
                            stored_anexo = await existing_document(
                                ext_key, "anexo", source_id, anexo_i,
                            )
                            if stored_anexo:
                                remember_document(ext_key, stored_anexo)
                                async with self._proxy_usage.track(
                                    operation="anexo_document",
                                    law_firm_id=case["law_firm_id"],
                                    case_id=case["id"],
                                    sync_run_id=sync_run_id,
                                    movement_id=movement_ids.get(ext_key),
                                    transaction_key=f"{sync_run_id}:anexo:{ext_key}:{source_id}:skip",
                                    estimated_bytes=0,
                                ) as usage:
                                    usage.documents_skipped += 1
                                continue

                            async with self._proxy_usage.track(
                                operation="anexo_document",
                                law_firm_id=case["law_firm_id"],
                                case_id=case["id"],
                                sync_run_id=sync_run_id,
                                movement_id=movement_ids.get(ext_key),
                                transaction_key=f"{sync_run_id}:anexo:{ext_key}:{source_id}",
                            ) as usage:
                                anexo_doc = await download_single_document(
                                    session,
                                    anexo_file["download_url"],
                                    anexo_file["download_token"],
                                    anexo_file.get("download_param", "dtaDoc"),
                                )
                                if anexo_doc:
                                    usage.documents_downloaded += 1
                            if not anexo_doc:
                                continue

                            r2_key = f"{case['law_firm_id']}/{case['id']}/{ext_key}-anexo-{anexo_i}.{anexo_doc.extension}"
                            await self._r2.upload(r2_key, anexo_doc.data, anexo_doc.content_type)
                            remember_document(ext_key, {
                                "type": "anexo",
                                "source_id": source_id,
                                "storage_key": r2_key,
                                "content_type": anexo_doc.content_type,
                                "label": anexo_file.get("label", "Anexo"),
                                "codigo": anexo_file.get("codigo"),
                            })
                            logger.info("Uploaded anexo %s (%d bytes)", r2_key, len(anexo_doc.data))
                        except Exception as exc:
                            if is_proxy_billing_error(exc) or is_proxy_cost_control_error(exc):
                                raise
                            logger.warning("Failed to download/upload anexo %d for %s", anexo_i, ext_key, exc_info=True)

                except Exception as exc:
                    if is_proxy_billing_error(exc) or is_proxy_cost_control_error(exc):
                        raise
                    logger.warning("Failed to resolve anexo list for %s (func=%s)", ext_key, anexo_func, exc_info=True)
                    # Anexo failure must NOT block primary or certs

            # --- Atomic DB update: write documents + document_storage_key together ---
            for ext_key, doc_list in per_movement_docs.items():
                update_data: dict = {"documents": doc_list}
                # Primary doc is always first in the list (if it exists)
                primary = next((d for d in doc_list if d["type"] == "principal"), None)
                if primary:
                    update_data["document_storage_key"] = primary["storage_key"]
                    update_data["document_content_type"] = primary["content_type"]

                try:
                    await run_query(
                        self._sb.from_("case_movements")
                        .update(update_data)
                        .eq("case_id", case["id"])
                        .eq("external_movement_key", ext_key)
                    )
                except Exception as exc:
                    if is_proxy_billing_error(exc) or is_proxy_cost_control_error(exc):
                        raise
                    logger.warning("Failed to update documents for %s", ext_key, exc_info=True)

        except Exception as exc:
            if is_proxy_billing_error(exc) or is_proxy_cost_control_error(exc):
                raise
            logger.warning("Document download/upload failed for case %s", case["case_number"], exc_info=True)

    async def _load_existing_movements(self, case_id: str) -> list[dict]:
        movements: list[dict] = []
        start = 0
        while True:
            response = await run_query(
                self._sb.from_("case_movements")
                .select("id,external_movement_key,raw_payload,documents,document_storage_key,document_content_type")
                .eq("case_id", case_id)
                .order("id")
                .range(start, start + _MOVEMENT_PAGE_SIZE - 1)
            )
            page = response.data if isinstance(response.data, list) else []
            movements.extend(page)
            if len(page) < _MOVEMENT_PAGE_SIZE:
                return movements
            start += _MOVEMENT_PAGE_SIZE

    async def _finish_run(
        self,
        run_id,
        started_at,
        status,
        new_movements,
        error=None,
        *,
        error_code=None,
    ):
        if not run_id:
            return
        now = datetime.now(TZ_SANTIAGO)
        duration_ms = int((now - started_at).total_seconds() * 1000)
        error_message = safe_error(error)[:300] if error is not None else None
        resolved_error_code = error_code or _sync_run_error_code(error)
        if (
            resolved_error_code is not None
            and resolved_error_code not in _SYNC_RUN_ERROR_CODES
        ):
            resolved_error_code = "unknown_case_error"
        try:
            await run_query(
                self._sb.from_("case_sync_runs").update({
                    "status": status,
                    "finished_at": now.isoformat(),
                    "duration_ms": duration_ms,
                    "new_movements_count": new_movements,
                    "error_message": error_message,
                    "error_code": resolved_error_code,
                }).eq("id", run_id)
            )
        except Exception:
            logger.exception("Failed to finish sync_run %s", run_id)

    async def _handle_blocked(
        self, case_id: str, cause: BlockCause, detail: str | None = None
    ):
        """Maneja un bloqueo SIN penalizar la causa, dejando dicho QUIÉN bloqueó.

        Marca la causa como blocked (sin incrementar consecutive_sync_failures) y abre el
        circuit breaker con una pausa corta. El re-mint de cookies ya NO se
        hace aquí: ocurre por-slot, de forma reactiva, cuando `sync_case`
        libera la sesión con `release(session, healthy=False)` — el slot que
        realmente vio el bloqueo es el único que se re-mintea, y solo cuando
        su dueño (esta corrutina) lo devuelve al pool.

        `cause` separa el bloqueo del WAF de OJV ("ojv") de una caída nuestra
        camino a OJV ("infra"): timeout de transporte, pool sin bundle F5, sesión
        Familia que no levanta. Los dos siguen sin penalizar a la causa —eso no
        cambia—, pero antes los dos escribían "Acceso bloqueado por OJV" en
        `last_sync_error` y la app se lo mostraba al abogado tal cual: le
        echábamos la culpa al Poder Judicial de fallas nuestras.

        Sin default, igual que del lado de la app: los cinco call sites saben
        cuál es, y un default los dejaría elegir por omisión — que es exactamente
        como el texto de OJV terminó cubriendo los dos casos. Acá pesa más que
        allá porque este repo no tiene CI ni type-checker: `BlockCause` documenta
        el dominio, pero lo único que impide la omisión es que la firma la exija.
        """
        await self._update_case_blocked(case_id, cause, detail)
        self._backoff.record_blocked()

    async def _handle_parse_suspect(self, case: dict, competencia: str):
        """Maneja una página recibida-pero-no-parseable SIN penalizar la causa
        ni tocar su external_payload. Reintenta en ~1h y alerta a ops (con
        cooldown) para intervención humana (drift de F5/OJV o del parser)."""
        retry_at = (datetime.now(TZ_SANTIAGO) + timedelta(seconds=_PARSE_RETRY_S)).isoformat()
        await run_query(
            self._sb.from_("cases").update({
                "last_sync_status": "parse_error",
                "last_sync_error": "Página recibida pero no parseable (revisar parser/F5)",
                "next_sync_at": retry_at,
            }).eq("id", case["id"])
        )
        now = time.monotonic()
        if now - self._last_parse_alert_at < _PARSE_ALERT_COOLDOWN_S:
            logger.warning("Parse-failure (alerta en cooldown): case=%s comp=%s", case["id"], competencia)
            return
        self._last_parse_alert_at = now
        logger.warning("Parse-failure en página real: case=%s comp=%s", case["id"], competencia)
        await send_ops_alert(
            self._config.TELEGRAM_BOT_TOKEN, self._config.TELEGRAM_CHAT_ID,
            "parse_failed",
            f"Causa {case.get('external_case_number') or case['id']} ({competencia}): "
            f"página recibida pero el parser no extrajo nada. Revisar forma de la página / parser.",
        )

    async def _update_case_blocked(
        self, case_id: str, cause: BlockCause, detail: str | None = None
    ):
        blocked_until = (datetime.now(TZ_SANTIAGO) + timedelta(seconds=_BLOCK_DURATION_S)).isoformat()
        await run_query(
            self._sb.from_("cases").update({
                "tracking_status": "blocked",
                "last_sync_status": "blocked",
                "last_sync_error": blocked_error_message(cause, detail),
                "sync_blocked_until": blocked_until,
            }).eq("id", case_id)
        )

    async def _update_case_error(self, case: dict, error: str):
        """Update case with error status and escalating backoff.

        Backoff schedule based on consecutive_sync_failures:
          1st error: 5 minutes
          2nd error: 30 minutes
          3rd error: 2 hours
          4th+: 6 hours
          After 10 failures: suspended (irrecoverable)

        Recibe la FILA, no `case["id"]` + el contador por separado: el contador
        venia como tercer parametro desde 6 call sites, y olvidarlo caia al
        default 0 en silencio — la causa reiniciaba el backoff a 5 minutos para
        siempre y NUNCA se suspendia. Con la fila no queda nada que olvidar.

        `case["consecutive_sync_failures"]` se indexa sin default a proposito.
        La fila viene de `Scheduler.get_next_batch()`, que hace `select("*")`
        sobre una columna NOT NULL DEFAULT 0, asi que la clave siempre esta; si
        algun dia falta es drift de esquema, y ahi el KeyError es lo que
        queremos. `_run_one` en __main__ lo atrapa por causa y lo loguea con
        `logger.exception`, y el watchdog alerta al ver 3 tracebacks en una hora
        — ruidoso y contenido, en vez de silencioso y permanente.
        """
        _MAX_CONSECUTIVE_FAILURES = 10
        case_id = case["id"]
        consecutive_sync_failures = case["consecutive_sync_failures"]

        if consecutive_sync_failures >= _MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                "Case %s reached %d consecutive sync failures, suspending",
                case_id, _MAX_CONSECUTIVE_FAILURES,
            )
            await run_query(
                self._sb.from_("cases").update({
                    "tracking_status": "suspended",
                    "last_sync_status": "error",
                    "last_sync_error": f"Suspended after {consecutive_sync_failures} consecutive failures: {error}",
                    "consecutive_sync_failures": consecutive_sync_failures + 1,
                }).eq("id", case_id)
            )
            return

        backoff_seconds = {0: 300, 1: 1800, 2: 7200}.get(
            consecutive_sync_failures, 21600
        )
        blocked_until = (datetime.now(TZ_SANTIAGO) + timedelta(seconds=backoff_seconds)).isoformat()

        await run_query(
            self._sb.from_("cases").update({
                "tracking_status": "error",
                "last_sync_status": "error",
                "last_sync_error": error,
                "sync_blocked_until": blocked_until,
                "consecutive_sync_failures": consecutive_sync_failures + 1,
            }).eq("id", case_id)
        )
