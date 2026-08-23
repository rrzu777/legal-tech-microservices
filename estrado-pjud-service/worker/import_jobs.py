"""Durable, fenced worker for OJV ``Mis Causas`` discovery jobs.

This module is deliberately isolated from scheduled public case sync.  It only
claims/finalizes import staging rows and never reads or writes ``cases``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, UUID4

from app.familia.auth import (
    FamiliaAuthSession,
    InvalidCredentialsError,
)
from app.my_causes.client import DiscoveryResult, discover_my_causes
from app.my_causes.models import ImportCandidate, Matter
from worker.config import run_query


logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 1_000
_MAX_CANDIDATE_BYTES = 4_096
_MAX_PAYLOAD_BYTES = 1_048_576
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


def _normalize_identity_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    collapsed = " ".join(normalized.split()).upper()
    return collapsed or None


def _source_hash(candidate: ImportCandidate) -> str:
    """Hash canonical identity only; captions, labels and row order are mutable."""
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
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candidate_payloads(raw_candidates: list[Any]) -> list[dict[str, Any]]:
    if len(raw_candidates) > _MAX_CANDIDATES:
        raise ValueError("too_many_import_candidates")

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

    payloads = list(payloads_by_hash.values())
    if len(json.dumps(payloads, ensure_ascii=False).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("import_candidates_payload_too_large")
    return payloads


_ERRORS: dict[str, tuple[str, str, str]] = {
    "credential_invalid": ("failed", "credential_invalid", "authentication"),
    "session_expired": ("failed", "session_expired", "authentication"),
    "waf": ("failed", "ojv_blocked", "transport"),
    "timeout": ("failed", "pjud_timeout", "transport"),
    "upstream_changed": ("partial", "upstream_changed", "upstream"),
}


def _summary(result: DiscoveryResult, candidate_count: int) -> dict[str, Any]:
    if result.status == "ok":
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
        pool,
        worker_id: str,
        fetch_credential: Callable[[str, str], Awaitable[dict | None]],
        discover: Callable[..., Awaitable[DiscoveryResult]] = discover_my_causes,
        session_factory=FamiliaAuthSession,
        concurrency: int = 1,
        lease_seconds: int = 120,
        renewal_interval_seconds: float | None = None,
    ):
        if concurrency < 1:
            raise ValueError("import_concurrency_must_be_positive")
        if not 15 <= lease_seconds <= 900:
            raise ValueError("import_lease_seconds_out_of_range")
        self._sb = supabase
        self._pool = pool
        self._worker_id = worker_id
        self._fetch_credential = fetch_credential
        self._discover = discover
        self._session_factory = session_factory
        self._semaphore = asyncio.Semaphore(concurrency)
        self._lease_seconds = lease_seconds
        self._renewal_interval_seconds = (
            renewal_interval_seconds
            if renewal_interval_seconds is not None
            else max(5.0, lease_seconds / 3)
        )

    async def _rpc(self, name: str, payload: dict[str, Any]) -> Any:
        response = await run_query(self._sb.rpc(name, payload))
        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(f"{name}_failed")
        return getattr(response, "data", None)

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

    async def _renew_until_cancelled(self, job: ClaimedImportJob) -> None:
        while True:
            await asyncio.sleep(self._renewal_interval_seconds)
            renewed = await self._rpc(
                "renew_pjud_import_job_claim",
                {
                    "p_job_id": str(job.job_id),
                    "p_claim_token": str(job.claim_token),
                    "p_worker_id": self._worker_id,
                    "p_lease_seconds": self._lease_seconds,
                },
            )
            if renewed is not True:
                raise RuntimeError("import_job_claim_lost")

    async def _run_fenced(self, job: ClaimedImportJob, operation: Awaitable[DiscoveryResult]) -> DiscoveryResult:
        operation_task = asyncio.create_task(operation)
        renewal_task = asyncio.create_task(self._renew_until_cancelled(job))
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

    async def _discover_once(self, job: ClaimedImportJob, credential: dict) -> DiscoveryResult:
        bundle, slot = await self._pool.acquire_familia_bundle()
        if bundle is None:
            await self._pool.release_familia_bundle(slot, disposition="healthy")
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
                try:
                    await session.login(
                        credential["rut"], credential["password"], "clave_pj",
                    )
                except InvalidCredentialsError:
                    return DiscoveryResult(
                        candidates=[], page_count=0, status="credential_invalid",
                    )
                return await self._discover(
                    session,
                    job.matters,
                    job.include_closed,
                )
        except asyncio.CancelledError:
            # The borrowed F5 bundle is read-only and the authenticated client
            # is ephemeral. Shutdown must not trigger a paid reactive remint.
            disposition = "healthy"
            remint = False
            raise
        except BaseException:
            disposition = "replace_before_reuse"
            raise
        finally:
            await self._pool.release_familia_bundle(
                slot, disposition=disposition, remint=remint,
            )

    async def _discover_with_session_retry(
        self, job: ClaimedImportJob, credential: dict,
    ) -> DiscoveryResult:
        result = await self._discover_once(job, credential)
        if result.status != "session_expired":
            return result
        return await self._discover_once(job, credential)

    async def process_next(self) -> bool:
        async with self._semaphore:
            job = await self._claim()
            if job is None:
                return False
            await self._process_claimed_with_budget(job)
            return True

    async def _fetch_and_discover(self, job: ClaimedImportJob) -> DiscoveryResult:
        credential = await self._fetch_credential(
            str(job.credential_id), str(job.law_firm_id),
        )
        if not credential or credential.get("password_type") != "clave_poder_judicial":
            return DiscoveryResult(
                candidates=[], page_count=0, status="credential_invalid",
            )
        return await self._discover_with_session_retry(job, credential)

    async def process_claimed(self, raw_job: ClaimedImportJob | dict[str, Any]) -> None:
        job = (
            raw_job
            if isinstance(raw_job, ClaimedImportJob)
            else ClaimedImportJob.model_validate(raw_job)
        )
        async with self._semaphore:
            await self._process_claimed_with_budget(job)

    async def _process_claimed_with_budget(self, job: ClaimedImportJob) -> None:
        result = await self._run_fenced(job, self._fetch_and_discover(job))
        try:
            candidates = _candidate_payloads(list(result.candidates))
            summary = _summary(result, len(candidates))
        except ValueError:
            candidates = []
            summary = {
                "status": "failed",
                "pages": max(0, int(getattr(result, "page_count", 0))),
                "discovered": 0,
                "error_code": "invalid_candidate_payload",
                "error_class": "contract",
            }
        await self._rpc(
            "finalize_pjud_import_discovery",
            {
                "p_job_id": str(job.job_id),
                "p_claim_token": str(job.claim_token),
                "p_candidates": candidates,
                "p_summary": summary,
            },
        )
        logger.info(
            "my_causes job=%s status=%s pages=%d count=%d",
            job.job_id,
            result.status,
            result.page_count,
            len(candidates),
        )
