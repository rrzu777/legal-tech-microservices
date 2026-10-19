"""Closed claim-bound contract for authenticated Civil/Appeals import detail."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator

from app.models import PenalBookSafeDetail
from app.my_causes.models import ImportCandidate


class AuthenticatedImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID4
    claim_token: UUID4
    worker_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    law_firm_id: UUID4
    credential_id: UUID4
    credential_binding_version: datetime

    @field_validator("credential_binding_version")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("credential_revision_requires_timezone")
        return value


class AuthenticatedImportResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: ImportCandidate
    detail: PenalBookSafeDetail
    credential_binding_version: datetime


PrivateError = Literal[
    "private_not_found", "private_ambiguous", "private_identifier_mismatch",
    "private_tribunal_mismatch", "private_evidence_incomplete",
    "private_fence_unavailable", "credential_invalid", "session_expired",
    "waf", "timeout", "upstream_changed", "pjud_unavailable",
]


class AuthenticatedImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    resolution: AuthenticatedImportResolution | None
    error_code: PrivateError | None


class AuthenticatedImportError(Exception):
    def __init__(self, code: PrivateError):
        self.code = code
        super().__init__(code)
