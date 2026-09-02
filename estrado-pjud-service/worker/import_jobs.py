"""Durable, fenced worker for OJV ``Mis Causas`` discovery jobs.

This module is deliberately isolated from scheduled public case sync.  It only
claims/finalizes import staging rows and never reads or writes ``cases``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar, cast

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, UUID4

from app.familia.auth import FamiliaAuthSession
from app.my_causes.client import DiscoveryResult, DiscoveryStatus, discover_my_causes
from app.my_causes.models import ImportCandidate, Matter
from app.ojv.errors import OjvSessionError
from app.ojv.budget import OjvLaneBudget
from app.proxy_billing import ProxyBillingExhaustedError
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.runtime_fence import validate_runtime_generation
from worker.config import run_query
from worker.proxy_usage import DISABLED_PROXY_USAGE, ProxyUsageTracker
from worker.trial_scope import TrialScope


logger = logging.getLogger(__name__)
_T = TypeVar("_T")

_MAX_CANDIDATES = 1_000
_MAX_CANDIDATE_BYTES = 4_096
_MAX_PAYLOAD_BYTES = 1_048_576
_TRIAL_GRANT_CLOSE_REPLAY_DELAYS_S = (0.1,)
_TRIAL_DISCOVERY_FINALIZE_REPLAY_DELAYS_S = (0.1,)
_TRIAL_IMPORT_CLAIM_REPLAY_DELAYS_S = (0.1,)
_TRIAL_GRANT_CLOSE_FIELDS = frozenset({
    "status",
    "job_status",
    "summary_status",
    "discovered_count",
    "candidate_count",
    "evidence_sha256",
})
_TRIAL_JOB_STATUSES = frozenset({
    "needs_selection",
    "completed",
    "partial",
    "failed",
})
_TRIAL_SUMMARY_STATUSES = frozenset({"needs_selection", "partial", "failed"})
_EVIDENCE_SHA256 = re.compile(r"[0-9a-f]{64}")
# These failures can lose a committed response. Connect/pool failures are
# pre-send and do not need an authority-bearing replay.
_AMBIGUOUS_TRIAL_GRANT_CLOSE_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
)
_CANDIDATE_FIELDS = frozenset({
    "matter",
    "case_type",
    "case_number",
    "court_code",
    "court_label",
    "tribunal_code",
    "tribunal_label",
    "libro",
    "filed_at",
    "upstream_status",
    "caption",
})
_PERSISTED_TEXT_LIMITS = {
    "case_number": 128,
    "court_label": 200,
    "tribunal_label": 200,
    "libro": 80,
    "upstream_status": 100,
    "caption": 500,
}


class ClaimedImportJob(BaseModel):
    """Exact output contract of ``claim_pjud_import_job`` when acquired."""

    model_config = ConfigDict(extra="forbid")

    status: str
    job_id: UUID4
    law_firm_id: UUID4
    credential_id: UUID4
    matters: tuple[Matter, ...]
    include_closed: bool
    claim_token: UUID4
    lease_expires_at: str


class ClaimedTrialImportJob(ClaimedImportJob):
    """Exact output contract of the capability-scoped trial claim."""

    trial_grant_id: UUID4
    expected_credentials_updated_at: datetime


@dataclass(frozen=True)
class TrialImportOutcome:
    """DB-confirmed result after exact finalize and terminal grant close."""

    claimed: bool
    job_id: str | None
    job_status: str | None
    summary_status: str | None
    discovered_count: int
    candidate_count: int
    evidence_sha256: str | None

    @property
    def successful(self) -> bool:
        return (
            self.claimed
            and self.job_id is not None
            and self.job_status == "needs_selection"
            and self.summary_status == "needs_selection"
            and self.discovered_count == self.candidate_count
            and self.discovered_count > 0
            and isinstance(self.evidence_sha256, str)
            and _EVIDENCE_SHA256.fullmatch(self.evidence_sha256) is not None
        )

    @classmethod
    def from_close_proof(
        cls,
        raw: Any,
        *,
        job_id: str,
    ) -> TrialImportOutcome:
        if type(raw) is not dict or set(raw) != _TRIAL_GRANT_CLOSE_FIELDS:
            raise ValueError("invalid_trial_grant_close_contract")
        if raw["status"] != "trial_grant_closed":
            raise ValueError("invalid_trial_grant_close_contract")
        if (
            type(raw["job_status"]) is not str
            or raw["job_status"] not in _TRIAL_JOB_STATUSES
            or type(raw["summary_status"]) is not str
            or raw["summary_status"] not in _TRIAL_SUMMARY_STATUSES
        ):
            raise ValueError("invalid_trial_grant_close_contract")
        discovered_count = raw["discovered_count"]
        candidate_count = raw["candidate_count"]
        if (
            type(discovered_count) is not int
            or discovered_count < 0
            or type(candidate_count) is not int
            or candidate_count < 0
        ):
            raise ValueError("invalid_trial_grant_close_contract")
        evidence_sha256 = raw["evidence_sha256"]
        if (
            type(evidence_sha256) is not str
            or _EVIDENCE_SHA256.fullmatch(evidence_sha256) is None
        ):
            raise ValueError("invalid_trial_grant_close_contract")
        return cls(
            claimed=True,
            job_id=job_id,
            job_status=raw["job_status"],
            summary_status=raw["summary_status"],
            discovered_count=discovered_count,
            candidate_count=candidate_count,
            evidence_sha256=evidence_sha256,
        )

    @classmethod
    def empty(cls) -> TrialImportOutcome:
        return cls(
            claimed=False,
            job_id=None,
            job_status=None,
            summary_status=None,
            discovered_count=0,
            candidate_count=0,
            evidence_sha256=None,
        )


def _normalize_identity_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    collapsed = " ".join(normalized.split()).upper()
    return collapsed or None


def _source_hash(candidate: ImportCandidate) -> str:
    """Hash canonical identity plus evidence needed while territory is unknown.

    Abbreviated listing labels can represent tribunals in several regions. In
    that provisional state the caption is not canonical case identity, but it
    must keep otherwise indistinguishable source rows apart until the selected
    candidate is enriched. Once official codes are present it is excluded.
    """
    needs_evidence_discriminator = (
        candidate.matter != "suprema"
        and (
            candidate.court_code is None
            or (
                candidate.matter not in {"apelaciones"}
                and candidate.tribunal_code is None
            )
            or (candidate.matter == "apelaciones" and candidate.libro is None)
        )
    )
    identity = [
        candidate.matter,
        candidate.case_type,
        _normalize_identity_text(candidate.case_number),
        candidate.court_code,
        None if candidate.court_code is not None else _normalize_identity_text(candidate.court_label),
        candidate.tribunal_code,
        None if candidate.tribunal_code is not None else _normalize_identity_text(candidate.tribunal_label),
        _normalize_identity_text(candidate.libro),
    ]
    if needs_evidence_discriminator:
        identity.extend([
            _normalize_identity_text(candidate.caption),
            candidate.filed_at.isoformat() if candidate.filed_at else None,
        ])
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CandidatePayloadBatch:
    payloads: list[dict[str, Any]]
    total_unique: int
    truncated: bool


def _candidate_payloads(raw_candidates: list[Any]) -> CandidatePayloadBatch:

    payloads_by_hash: dict[str, dict[str, Any]] = {}
    for raw_candidate in raw_candidates:
        try:
            candidate = ImportCandidate.model_validate(raw_candidate)
        except Exception as exc:
            raise ValueError("invalid_import_candidate") from exc
        payload = candidate.model_dump(mode="json")
        if set(payload) != _CANDIDATE_FIELDS:
            raise ValueError("invalid_import_candidate_allowlist")
        if any(
            isinstance(payload.get(field), str)
            and len(payload[field]) > limit
            for field, limit in _PERSISTED_TEXT_LIMITS.items()
        ):
            raise ValueError("invalid_import_candidate_field_limit")
        source_hash = _source_hash(candidate)
        payload["source_hash"] = source_hash
        if any(isinstance(value, (dict, list)) for value in payload.values()):
            raise ValueError("invalid_import_candidate_nested_value")
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > _MAX_CANDIDATE_BYTES:
            raise ValueError("import_candidate_too_large")
        existing = payloads_by_hash.get(source_hash)
        if existing is None:
            payloads_by_hash[source_hash] = payload
        else:
            # Replay/duplicate rows may carry fresher display metadata. Identity
            # stays hash-stable while the last non-null value wins.
            payloads_by_hash[source_hash] = {
                key: value if value is not None else existing.get(key)
                for key, value in payload.items()
            }

    total_unique = len(payloads_by_hash)
    payloads: list[dict[str, Any]] = []
    encoded_size = 2  # Opening and closing brackets of the JSON array.
    for payload in payloads_by_hash.values():
        if len(payloads) >= _MAX_CANDIDATES:
            break
        encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        next_size = encoded_size + len(encoded_payload) + (2 if payloads else 0)
        if next_size > _MAX_PAYLOAD_BYTES:
            break
        payloads.append(payload)
        encoded_size = next_size
    return CandidatePayloadBatch(
        payloads=payloads,
        total_unique=total_unique,
        truncated=total_unique > len(payloads),
    )


_ERRORS: dict[str, tuple[str, str, str]] = {
    "credential_invalid": ("failed", "credential_invalid", "authentication"),
    "session_expired": ("failed", "session_expired", "authentication"),
    "waf": ("failed", "ojv_blocked", "transport"),
    "timeout": ("failed", "pjud_timeout", "transport"),
    "upstream_changed": ("partial", "upstream_changed", "upstream"),
}


def _summary(
    result: DiscoveryResult,
    candidate_count: int,
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    if result.status == "ok":
        if truncated:
            return {
                "status": "needs_selection",
                "pages": result.page_count,
                "discovered": candidate_count,
                "error_code": "candidate_limit_reached",
                "error_class": "limit",
            }
        return {
            "status": "needs_selection",
            "pages": result.page_count,
            "discovered": candidate_count,
        }
    status, error_code, error_class = _ERRORS[result.status]
    return {
        "status": status,
        "pages": result.page_count,
        "discovered": candidate_count,
        "error_code": error_code,
        "error_class": error_class,
    }


class ImportDiscoveryWorker:
    """Claim and process one import job with its own concurrency semaphore."""

    def __init__(
        self,
        *,
        supabase,
        trial_supabase=None,
        pool,
        worker_id: str,
        fetch_credential: Callable[[str, str, str, str, str], Awaitable[dict | None]],
        fetch_trial_credential: Callable[..., Awaitable[dict | None]] | None = None,
        discover: Callable[..., Awaitable[DiscoveryResult]] = discover_my_causes,
        session_factory=FamiliaAuthSession,
        concurrency: int = 1,
        lease_seconds: int = 120,
        renewal_interval_seconds: float | None = None,
        enabled: bool = True,
        lane_budget: OjvLaneBudget | None = None,
        proxy_usage: ProxyUsageTracker | None = None,
    ):
        if concurrency < 1:
            raise ValueError("import_concurrency_must_be_positive")
        if not 15 <= lease_seconds <= 900:
            raise ValueError("import_lease_seconds_out_of_range")
        self._sb = supabase
        self._trial_sb = trial_supabase
        self._pool = pool
        self._worker_id = worker_id
        self._fetch_credential = fetch_credential
        self._fetch_trial_credential = fetch_trial_credential
        self._discover = discover
        self._session_factory = session_factory
        self._lane_budget = lane_budget or OjvLaneBudget(concurrency)
        self._proxy_usage = proxy_usage or DISABLED_PROXY_USAGE
        if enabled and not self._proxy_usage.enabled:
            raise ValueError("import_proxy_usage_tracking_required")
        self._lease_seconds = lease_seconds
        self._renewal_interval_seconds = (
            renewal_interval_seconds
            if renewal_interval_seconds is not None
            else max(5.0, lease_seconds / 3)
        )
        self._enabled = enabled

    async def _rpc(self, name: str, payload: dict[str, Any]) -> Any:
        response = await run_query(self._sb.rpc(name, payload))
        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(f"{name}_failed")
        return getattr(response, "data", None)

    async def _trial_rpc(self, name: str, payload: dict[str, Any]) -> Any:
        if self._trial_sb is None:
            raise RuntimeError("trial_rpc_client_unavailable")
        response = await run_query(self._trial_sb.rpc(name, payload))
        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(f"{name}_failed")
        return getattr(response, "data", None)

    async def _close_trial_grant(
        self,
        scope: TrialScope,
    ) -> TrialImportOutcome:
        payload = {
            "p_expected_generation": str(scope.runtime_generation),
            "p_trial_grant_id": str(scope.trial_grant_id),
            "p_job_id": str(scope.job_id),
        }
        attempts = len(_TRIAL_GRANT_CLOSE_REPLAY_DELAYS_S) + 1
        for attempt in range(attempts):
            try:
                close_proof = await self._trial_rpc(
                    "close_pjud_runtime_trial_grant", payload,
                )
            except _AMBIGUOUS_TRIAL_GRANT_CLOSE_ERRORS:
                if attempt == attempts - 1:
                    raise RuntimeError(
                        "pjud_trial_grant_close_unconfirmed"
                    ) from None
                logger.warning(
                    "Ambiguous PJUD trial grant close transport; "
                    "replaying exact RPC"
                )
                await asyncio.sleep(
                    _TRIAL_GRANT_CLOSE_REPLAY_DELAYS_S[attempt]
                )
                continue
            except Exception:
                raise RuntimeError(
                    "pjud_trial_grant_close_unconfirmed"
                ) from None
            try:
                return TrialImportOutcome.from_close_proof(
                    close_proof,
                    job_id=str(scope.job_id),
                )
            except ValueError:
                raise RuntimeError(
                    "pjud_trial_grant_close_unconfirmed"
                ) from None

        raise RuntimeError("pjud_trial_grant_close_unconfirmed")

    async def _finalize_trial_discovery(self, payload: dict[str, Any]) -> None:
        """Replay only the exact idempotent finalize after an ambiguous send."""
        attempts = len(_TRIAL_DISCOVERY_FINALIZE_REPLAY_DELAYS_S) + 1
        ambiguity_observed = False
        for attempt in range(attempts):
            try:
                await self._trial_rpc(
                    "finalize_pjud_trial_import_discovery", payload,
                )
                return
            except _AMBIGUOUS_TRIAL_GRANT_CLOSE_ERRORS:
                ambiguity_observed = True
                if attempt == attempts - 1:
                    raise RuntimeError(
                        "pjud_trial_discovery_finalize_unconfirmed"
                    ) from None
                logger.warning(
                    "Ambiguous PJUD trial discovery finalize transport; "
                    "replaying exact RPC"
                )
                await asyncio.sleep(
                    _TRIAL_DISCOVERY_FINALIZE_REPLAY_DELAYS_S[attempt]
                )
            except Exception:
                if ambiguity_observed:
                    raise RuntimeError(
                        "pjud_trial_discovery_finalize_unconfirmed"
                    ) from None
                raise

        raise RuntimeError("pjud_trial_discovery_finalize_unconfirmed")

    async def _claim(self) -> ClaimedImportJob | None:
        data = await self._rpc(
            "claim_pjud_import_job",
            {
                "p_worker_id": self._worker_id,
                "p_lease_seconds": self._lease_seconds,
            },
        )
        if not isinstance(data, dict) or data.get("status") == "empty":
            return None
        try:
            job = ClaimedImportJob.model_validate(data)
        except Exception as exc:
            raise RuntimeError("invalid_import_job_claim_contract") from exc
        if job.status != "acquired":
            raise RuntimeError("invalid_import_job_claim_status")
        return job

    async def _claim_trial(
        self,
        *,
        capability: SecretStr,
        runtime_generation: str,
    ) -> tuple[ClaimedTrialImportJob, TrialScope] | None:
        payload = {
            "p_expected_generation": runtime_generation,
            "p_worker_id": self._worker_id,
            "p_lease_seconds": self._lease_seconds,
        }
        attempts = len(_TRIAL_IMPORT_CLAIM_REPLAY_DELAYS_S) + 1
        for attempt in range(attempts):
            try:
                data = await self._trial_rpc(
                    "claim_pjud_trial_import_job", payload,
                )
                break
            except _AMBIGUOUS_TRIAL_GRANT_CLOSE_ERRORS:
                if attempt == attempts - 1:
                    raise RuntimeError(
                        "pjud_trial_import_job_claim_unconfirmed"
                    ) from None
                logger.warning(
                    "Ambiguous PJUD trial claim transport; replaying exact RPC"
                )
                await asyncio.sleep(
                    _TRIAL_IMPORT_CLAIM_REPLAY_DELAYS_S[attempt]
                )
            except Exception:
                raise RuntimeError(
                    "pjud_trial_import_job_claim_unconfirmed"
                ) from None
        else:  # pragma: no cover - finite loop either breaks or raises.
            raise RuntimeError("pjud_trial_import_job_claim_unconfirmed")
        if data == {"status": "empty"}:
            return None
        try:
            job = ClaimedTrialImportJob.model_validate(data)
            scope = TrialScope(
                capability=capability,
                runtime_generation=runtime_generation,
                trial_grant_id=job.trial_grant_id,
                job_id=job.job_id,
                claim_token=job.claim_token,
                worker_id=self._worker_id,
                law_firm_id=job.law_firm_id,
                credential_id=job.credential_id,
                expected_credentials_updated_at=(
                    job.expected_credentials_updated_at
                ),
            )
        except Exception as exc:
            raise RuntimeError("invalid_trial_import_job_claim_contract") from exc
        if job.status != "acquired":
            raise RuntimeError("invalid_trial_import_job_claim_status")
        return job, scope

    async def _renew_until_cancelled(
        self,
        job: ClaimedImportJob,
        trial_scope: TrialScope | None = None,
    ) -> None:
        while True:
            await asyncio.sleep(self._renewal_interval_seconds)
            payload = {
                "p_job_id": str(job.job_id),
                "p_claim_token": str(job.claim_token),
                "p_worker_id": self._worker_id,
                "p_lease_seconds": self._lease_seconds,
            }
            rpc_name = "renew_pjud_import_job_claim"
            if trial_scope is not None:
                payload["p_expected_generation"] = str(
                    trial_scope.runtime_generation
                )
                payload["p_trial_grant_id"] = str(trial_scope.trial_grant_id)
                rpc_name = "renew_pjud_trial_import_job_claim"
            rpc = self._trial_rpc if trial_scope is not None else self._rpc
            renewed = await rpc(rpc_name, payload)
            if renewed is not True:
                raise RuntimeError("import_job_claim_lost")

    async def _run_fenced(
        self,
        job: ClaimedImportJob,
        operation: Awaitable[_T],
        trial_scope: TrialScope | None = None,
    ) -> _T:
        operation_task = asyncio.create_task(operation)
        renewal_task = asyncio.create_task(
            self._renew_until_cancelled(job, trial_scope),
        )
        try:
            done, _ = await asyncio.wait(
                {operation_task, renewal_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done:
                await renewal_task
                raise RuntimeError("import_job_claim_lost")
            return await operation_task
        finally:
            for task in (operation_task, renewal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation_task, renewal_task, return_exceptions=True)

    async def _validate_credential_revision(
        self,
        job: ClaimedImportJob,
        binding_version: str,
        trial_scope: TrialScope | None = None,
    ) -> None:
        payload = {
            "p_job_id": str(job.job_id),
            "p_claim_token": str(job.claim_token),
            "p_worker_id": self._worker_id,
            "p_credential_id": str(job.credential_id),
            "p_expected_credential_updated_at": binding_version,
        }
        rpc_name = "validate_pjud_import_credential_claim"
        if trial_scope is not None:
            payload["p_expected_generation"] = str(
                trial_scope.runtime_generation
            )
            payload["p_trial_grant_id"] = str(trial_scope.trial_grant_id)
            payload.pop("p_expected_credential_updated_at")
            payload["p_expected_credential_updated_at"] = (
                trial_scope.expected_credentials_updated_at.isoformat()
            )
            rpc_name = "validate_pjud_trial_import_credential_claim"
        rpc = self._trial_rpc if trial_scope is not None else self._rpc
        valid = await rpc(rpc_name, payload)
        if valid is not True:
            raise RuntimeError("import_credential_revision_lost")

    async def _discover_once(
        self,
        job: ClaimedImportJob,
        credential: dict,
        session_attempt: int,
        trial_scope: TrialScope | None = None,
    ) -> DiscoveryResult:
        if trial_scope is None:
            bundle, slot = await self._pool.acquire_familia_bundle()
        else:
            bundle, slot = await self._pool.acquire_familia_bundle(
                trial_scope=trial_scope,
            )
        if bundle is None:
            if trial_scope is None:
                await self._pool.release_familia_bundle(
                    slot, disposition="healthy",
                )
            else:
                await self._pool.release_familia_bundle(
                    slot,
                    disposition="healthy",
                    remint=False,
                    trial_scope=trial_scope,
                )
            raise RuntimeError("import_session_unavailable")
        disposition = "healthy"
        remint = True
        try:
            async with self._session_factory(
                bundle.proxy_url,
                bundle.cookies,
                bundle.user_agent,
                rate_limit_s=2.5,
            ) as session:
                common_usage = {
                    "law_firm_id": str(job.law_firm_id),
                    "import_job_id": str(job.job_id),
                    "import_claim_token": str(job.claim_token),
                    "import_worker_id": self._worker_id,
                }

                def request_scope(request_key: str):
                    usage = {
                        "operation": "search",
                        **common_usage,
                        "transaction_key": (
                            f"{job.job_id}:{job.claim_token}:session-{session_attempt}:"
                            f"page:{request_key}"
                        ),
                    }
                    if trial_scope is not None:
                        usage["trial_scope"] = trial_scope
                    return self._proxy_usage.track(
                        **usage,
                    )

                try:
                    await self._validate_credential_revision(
                        job, credential["binding_version"], trial_scope,
                    )
                    login_usage = {
                        "operation": "other",
                        **common_usage,
                        "transaction_key": (
                            f"{job.job_id}:{job.claim_token}:session-{session_attempt}:login"
                        ),
                    }
                    if trial_scope is not None:
                        login_usage["trial_scope"] = trial_scope
                    async with self._proxy_usage.track(**login_usage):
                        await session.login(
                            SecretStr(credential["rut"]),
                            SecretStr(credential["password"]),
                            "clave_pj",
                        )
                except OjvSessionError as error:
                    if error.code.value in {"session_expired", "waf", "timeout"}:
                        disposition = "replace_before_reuse"
                    return DiscoveryResult(
                        candidates=[],
                        page_count=0,
                        status=cast(DiscoveryStatus, error.code.value),
                    )
                await self._validate_credential_revision(
                    job, credential["binding_version"], trial_scope,
                )
                return await self._discover(
                    session,
                    job.matters,
                    job.include_closed,
                    request_scope=request_scope,
                )
        except asyncio.CancelledError:
            # The borrowed F5 bundle is read-only and the authenticated client
            # is ephemeral. Shutdown must not trigger a paid reactive remint.
            disposition = "healthy"
            remint = False
            raise
        except ProxyBillingExhaustedError:
            disposition = "replace_before_reuse"
            remint = False
            raise
        except (ProxyBudgetExceededError, ProxyUsagePersistenceError):
            # These boundaries fail before provider traffic or after telemetry
            # reconciliation. Neither outcome authorizes a paid reactive mint.
            disposition = "healthy"
            remint = False
            raise
        except BaseException:
            disposition = "replace_before_reuse"
            raise
        finally:
            release = {
                "disposition": disposition,
                "remint": remint,
            }
            if trial_scope is not None:
                release["trial_scope"] = trial_scope
            await self._pool.release_familia_bundle(slot, **release)

    async def _discover_with_session_retry(
        self,
        job: ClaimedImportJob,
        credential: dict,
        trial_scope: TrialScope | None = None,
    ) -> DiscoveryResult:
        result = await self._discover_once(job, credential, 1, trial_scope)
        if result.status != "session_expired":
            return result
        return await self._discover_once(job, credential, 2, trial_scope)

    async def process_next(self) -> bool:
        if not self._enabled:
            return False
        async with self._lane_budget.slot():
            job = await self._claim()
            if job is None:
                return False
            await self._process_claimed_with_budget(job)
            return True

    async def process_trial_next(
        self,
        *,
        capability: SecretStr,
        runtime_generation: str,
    ) -> TrialImportOutcome:
        if not self._enabled:
            return TrialImportOutcome.empty()
        generation = validate_runtime_generation(runtime_generation)
        if generation is None:
            raise ValueError("pjud_runtime_invalid_generation")
        async with self._lane_budget.slot():
            claimed = await self._claim_trial(
                capability=capability,
                runtime_generation=generation,
            )
            if claimed is None:
                return TrialImportOutcome.empty()
            job, scope = claimed
            await self._process_claimed_with_budget(job, scope)
            return await self._close_trial_grant(scope)

    async def _fetch_and_discover(
        self,
        job: ClaimedImportJob,
        trial_scope: TrialScope | None = None,
    ) -> tuple[DiscoveryResult, str | None]:
        credential_args = (
            str(job.credential_id), str(job.law_firm_id), str(job.job_id),
            str(job.claim_token), self._worker_id,
        )
        if trial_scope is None:
            credential = await self._fetch_credential(*credential_args)
        else:
            if self._fetch_trial_credential is None:
                raise RuntimeError("trial_import_credential_boundary_unavailable")
            credential = await self._fetch_trial_credential(
                *credential_args, trial_scope,
            )
        if not credential or credential.get("password_type") != "clave_poder_judicial":
            return DiscoveryResult(
                candidates=[], page_count=0, status="credential_invalid",
            ), None
        binding_version = credential.get("binding_version")
        if not isinstance(binding_version, str) or not binding_version:
            raise RuntimeError("invalid_import_credential_contract")
        if trial_scope is not None:
            try:
                observed_revision = datetime.fromisoformat(
                    binding_version.replace("Z", "+00:00"),
                )
            except ValueError as exc:
                raise RuntimeError(
                    "trial_import_credential_revision_mismatch"
                ) from exc
            if (
                observed_revision.utcoffset() is None
                or observed_revision
                != trial_scope.expected_credentials_updated_at
            ):
                raise RuntimeError("trial_import_credential_revision_mismatch")
            binding_version = (
                trial_scope.expected_credentials_updated_at.isoformat()
            )
        return await self._discover_with_session_retry(
            job, credential, trial_scope,
        ), binding_version

    async def process_claimed(self, raw_job: ClaimedImportJob | dict[str, Any]) -> None:
        job = (
            raw_job
            if isinstance(raw_job, ClaimedImportJob)
            else ClaimedImportJob.model_validate(raw_job)
        )
        async with self._lane_budget.slot():
            await self._process_claimed_with_budget(job)

    async def _process_claimed_with_budget(
        self,
        job: ClaimedImportJob,
        trial_scope: TrialScope | None = None,
    ) -> None:
        result, credential_binding_version = await self._run_fenced(
            job, self._fetch_and_discover(job, trial_scope), trial_scope,
        )
        try:
            batch = _candidate_payloads(list(result.candidates))
            candidates = batch.payloads
            summary = _summary(
                result,
                len(candidates),
                truncated=batch.truncated,
            )
        except ValueError:
            candidates = []
            summary = {
                "status": "failed",
                "pages": max(0, int(getattr(result, "page_count", 0))),
                "discovered": 0,
                "error_code": "invalid_candidate_payload",
                "error_class": "contract",
            }
        payload = {
            "p_job_id": str(job.job_id),
            "p_claim_token": str(job.claim_token),
            "p_candidates": candidates,
            "p_summary": summary,
            "p_worker_id": self._worker_id,
            "p_expected_credential_updated_at": credential_binding_version,
        }
        rpc_name = "finalize_pjud_import_discovery"
        if trial_scope is not None:
            payload["p_expected_generation"] = str(
                trial_scope.runtime_generation
            )
            payload["p_trial_grant_id"] = str(trial_scope.trial_grant_id)
            payload.pop("p_expected_credential_updated_at")
            payload["p_expected_credential_updated_at"] = (
                trial_scope.expected_credentials_updated_at.isoformat()
            )
        if trial_scope is None:
            await self._rpc(rpc_name, payload)
        else:
            await self._finalize_trial_discovery(payload)
        logger.info(
            "my_causes status=%s pages=%d count=%d",
            result.status,
            result.page_count,
            len(candidates),
        )
