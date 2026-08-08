"""Durable contracts for opportunistic PJUD catalog observations."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from worker.config import run_query

logger = logging.getLogger(__name__)

CatalogKind = Literal["tribunals", "books"]
CatalogOptions = list[dict[str, str]]


def canonical_catalog_options(options: object) -> CatalogOptions:
    """Return a stable, payload-safe catalog representation for hashing/storage."""
    if not isinstance(options, list):
        return []
    candidates: CatalogOptions = []
    for raw in options:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        label = " ".join(str(raw.get("label") or "").split())
        if code and label:
            candidates.append({"code": code, "label": label})

    canonical: CatalogOptions = []
    seen: set[str] = set()
    for option in sorted(candidates, key=lambda item: (item["code"], item["label"])):
        code = option["code"]
        if code in seen:
            continue
        seen.add(code)
        canonical.append(option)
    return canonical


def catalog_options_hash(options: object) -> str:
    payload = json.dumps(
        canonical_catalog_options(options),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_partial_catalog(snapshot: object, observed: object) -> bool:
    snapshot_codes = {option["code"] for option in canonical_catalog_options(snapshot)}
    observed_codes = {option["code"] for option in canonical_catalog_options(observed)}
    return not snapshot_codes.issubset(observed_codes)


@dataclass(frozen=True)
class CatalogRefreshIntent:
    slice_key: str
    catalog: CatalogKind
    competencia: str
    corte: int | None
    anno: int | None
    law_firm_id: str | None
    case_id: str | None
    sync_run_id: str | None
    request_hash: str

    def __post_init__(self) -> None:
        expected_prefix = f"{self.catalog}:"
        if not self.slice_key.startswith(expected_prefix):
            raise ValueError("catalog slice key does not match catalog kind")
        if len(self.request_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.request_hash
        ):
            raise ValueError("request hash must be lowercase sha256")

    def catalog_params(self) -> dict[str, str]:
        params = {"competencia": self.competencia}
        if self.corte is not None:
            params["corte"] = str(self.corte)
        if self.anno is not None:
            params["anno"] = str(self.anno)
        if self.catalog == "tribunals":
            params["tipo_busqueda"] = "1"
        return params

    def event_params(self, outcome: str) -> dict[str, str | None]:
        return {
            "p_slice_key": self.slice_key,
            "p_outcome": outcome,
            "p_law_firm_id": self.law_firm_id,
            "p_case_id": self.case_id,
            "p_request_hash": self.request_hash,
        }


@dataclass(frozen=True)
class CatalogClaim:
    slice_key: str
    token: uuid.UUID
    lease_expires_at: str | None
    intent: CatalogRefreshIntent | None = None


@dataclass(frozen=True)
class CatalogControl:
    opportunistic_enabled: bool
    circuit_open: bool


@dataclass(frozen=True)
class CatalogObservation:
    snapshot_hash: str
    snapshot_options: CatalogOptions
    observed_hash: str
    options: CatalogOptions
    session_generation_id: uuid.UUID
    bytes_up: int
    bytes_down: int
    partial: bool
    confirmed_by_full_refresh: bool = False


class CatalogObservationRepository:
    """Service-role RPC adapter; it never logs RPC parameters or result payloads."""

    def __init__(self, supabase, *, lease_seconds: int = 120, cooldown_seconds: int = 604_800):
        self._sb = supabase
        self._lease_seconds = lease_seconds
        self._cooldown_seconds = cooldown_seconds

    async def control(self) -> CatalogControl:
        response = await run_query(
            self._sb.from_("pjud_catalog_control")
            .select("opportunistic_enabled,circuit_open")
            .eq("singleton", True)
            .limit(1)
        )
        rows = response.data if isinstance(response.data, list) else []
        if len(rows) != 1:
            raise RuntimeError("catalog control did not return exactly one row")
        return CatalogControl(
            opportunistic_enabled=rows[0].get("opportunistic_enabled") is True,
            circuit_open=rows[0].get("circuit_open") is True,
        )

    async def claim(self, intent: CatalogRefreshIntent) -> CatalogClaim | None:
        token = uuid.uuid4()
        response = await run_query(self._sb.rpc("pjud_catalog_claim_refresh", {
            "p_slice_key": intent.slice_key,
            "p_claim_token": str(token),
            "p_lease_seconds": self._lease_seconds,
            "p_cooldown_seconds": self._cooldown_seconds,
        }))
        rows = response.data if isinstance(response.data, list) else []
        if len(rows) != 1:
            raise RuntimeError("catalog claim did not return exactly one row")
        row = rows[0]
        if row.get("allowed") is not True:
            reason = row.get("reason")
            if reason in {"cooldown", "lease_busy"}:
                try:
                    await self.record_event(intent, reason)
                except Exception:
                    logger.exception("Catalog claim-denial telemetry persistence failed")
            return None
        return CatalogClaim(
            slice_key=intent.slice_key,
            token=token,
            lease_expires_at=row.get("lease_expires_at"),
            intent=intent,
        )

    async def complete(
        self,
        claim: CatalogClaim,
        observation: CatalogObservation,
    ) -> None:
        intent = claim.intent
        if intent is None:
            raise ValueError("catalog completion requires its originating intent")
        await run_query(self._sb.rpc("pjud_catalog_complete_refresh", {
            "p_slice_key": claim.slice_key,
            "p_claim_token": str(claim.token),
            "p_snapshot_hash": observation.snapshot_hash,
            "p_snapshot_options": observation.snapshot_options,
            "p_observed_hash": observation.observed_hash,
            "p_options": observation.options,
            "p_request_hash": intent.request_hash,
            "p_session_generation_id": str(observation.session_generation_id),
            "p_law_firm_id": intent.law_firm_id,
            "p_case_id": intent.case_id,
            "p_sync_run_id": intent.sync_run_id,
            "p_bytes_up": max(0, observation.bytes_up),
            "p_bytes_down": max(0, observation.bytes_down),
            # Opportunistic slices are never evidence of a complete removal.
            "p_confirmed_by_full_refresh": (
                observation.confirmed_by_full_refresh and not observation.partial
            ),
        }))

    async def fail(self, claim: CatalogClaim, reason: str) -> None:
        await run_query(self._sb.rpc("pjud_catalog_fail_refresh", {
            "p_slice_key": claim.slice_key,
            "p_claim_token": str(claim.token),
            "p_reason": reason[:500],
        }))

    async def open_circuit(self, reason: str) -> None:
        await run_query(self._sb.rpc(
            "pjud_catalog_open_circuit",
            {"p_reason": reason[:500]},
        ))

    async def record_event(self, intent: CatalogRefreshIntent, outcome: str) -> None:
        await run_query(self._sb.rpc(
            "pjud_catalog_record_refresh_event",
            intent.event_params(outcome),
        ))
