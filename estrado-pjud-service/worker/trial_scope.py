"""Exact, immutable authority tuple for the bounded PJUD import trial."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, SecretStr, UUID4, field_validator

from app.runtime_fence import validate_runtime_generation


PJUD_RUNTIME_TRIAL_CAPABILITY_HEADER = "x-pjud-runtime-trial-capability"
PJUD_RUNTIME_TRIAL_GRANT_ID_HEADER = "X-Pjud-Runtime-Trial-Grant-Id"
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")


def validate_worker_id(value: str) -> str:
    if not isinstance(value, str) or _WORKER_ID.fullmatch(value) is None:
        raise ValueError("invalid_trial_worker")
    return value


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
        return validate_worker_id(value)

    @field_validator("runtime_generation", mode="before")
    @classmethod
    def _valid_runtime_generation(cls, value):
        generation = validate_runtime_generation(value)
        if generation is None:
            raise ValueError("pjud_runtime_invalid_generation")
        return generation

    @field_validator("expected_credentials_updated_at")
    @classmethod
    def _valid_credential_revision(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("invalid_trial_credential_revision")
        return value
