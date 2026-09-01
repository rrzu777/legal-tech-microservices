"""Exact, immutable authority tuple for the bounded PJUD import trial."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, SecretStr, UUID4, field_validator


PJUD_RUNTIME_TRIAL_CAPABILITY_HEADER = "x-pjud-runtime-trial-capability"
PJUD_RUNTIME_TRIAL_GRANT_ID_HEADER = "X-Pjud-Runtime-Trial-Grant-Id"


class TrialScope(BaseModel):
    """Claim-bound trial authority; the plaintext capability stays secret-safe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: SecretStr
    runtime_generation: UUID4
    trial_grant_id: UUID4
    job_id: UUID4
    claim_token: UUID4
    worker_id: str
    law_firm_id: UUID4
    credential_id: UUID4
    expected_credentials_updated_at: datetime

    @field_validator("capability")
    @classmethod
    def _valid_capability(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r"[0-9a-f]{64}", value.get_secret_value()) is None:
            raise ValueError("invalid_trial_capability")
        return value

    @field_validator("worker_id")
    @classmethod
    def _valid_worker_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
            raise ValueError("invalid_trial_worker")
        return value

    @field_validator("expected_credentials_updated_at")
    @classmethod
    def _valid_credential_revision(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("invalid_trial_credential_revision")
        return value
