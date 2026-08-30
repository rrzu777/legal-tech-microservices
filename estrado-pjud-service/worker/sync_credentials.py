"""Closed sync claim transport. No legacy reader, plaintext logging or direct writes."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Literal

import httpx
from postgrest.exceptions import APIError
from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, SecretStr, TypeAdapter,
    WithJsonSchema, model_validator,
)

from worker.config import run_query

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
ClaimUUID = Annotated[str, Field(pattern=_UUID)]
SecretRequestString = Annotated[SecretStr, WithJsonSchema({"type": "string"})]


def _version(value: str) -> str:
    if len(value) > 64 or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value,
    ):
        raise ValueError("invalid_credential_version")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value  # Never reserialize: microseconds are part of the fence.


CredentialVersion = Annotated[str, AfterValidator(_version)]


class SyncCredentialClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True, frozen=True)
    law_firm_id: ClaimUUID
    credential_id: ClaimUUID
    case_id: ClaimUUID
    run_id: ClaimUUID
    claim_kind: Literal["scheduled", "manual", "lookup"]
    claim_owner: str = Field(min_length=1, max_length=200)
    claim_token: SecretRequestString
    case_binding_version: int = Field(ge=0, le=9007199254740991)

    @model_validator(mode="after")
    def _bound_owner(self):
        token = self.claim_token.get_secret_value()
        valid = self.claim_owner.strip() == self.claim_owner and 1 <= len(token) <= 200
        if self.claim_kind == "scheduled":
            valid = valid and re.fullmatch(_UUID, token) and not self.claim_owner.startswith(("sync:", "lookup:"))
        else:
            prefix = "sync:" if self.claim_kind == "manual" else "lookup:"
            valid = valid and re.fullmatch(_UUID, self.claim_owner) and token.startswith(prefix) and len(token) > len(prefix)
        if not valid or len(json.dumps(self.rpc_context(), ensure_ascii=False, separators=(",", ":")).encode()) > 2048:
            raise ValueError("invalid_sync_credential_claim")
        return self

    def rpc_context(self) -> dict:
        """Explicit secret serialization only at authenticated transport boundaries."""
        return {**self.model_dump(exclude={"claim_token"}), "claim_token": self.claim_token.get_secret_value()}


class SyncCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True, frozen=True)
    rut: SecretRequestString = Field(min_length=1)
    password: SecretRequestString = Field(min_length=1)
    password_type: Literal["clave_poder_judicial"]
    credential_version: CredentialVersion


class SyncCredentialClaimStaleError(Exception):
    def __init__(self):
        super().__init__("sync_claim_stale")


class SyncCredentialInfrastructureError(Exception):
    def __init__(self):
        super().__init__("infra_unavailable")


class SyncCredentialClient:
    def __init__(self, supabase, config=None):
        self._sb = supabase
        self._config = config

    @staticmethod
    def _args(claim: SyncCredentialClaim, version: str | None) -> dict:
        if not isinstance(claim, SyncCredentialClaim):
            raise SyncCredentialInfrastructureError()
        try:
            if version is not None:
                TypeAdapter(CredentialVersion).validate_python(version, strict=True)
        except Exception:
            raise SyncCredentialInfrastructureError() from None
        return {"p_context": claim.rpc_context(), "p_credential_version": version}

    async def _rpc(self, name: str, args: dict):
        try:
            if self._sb is None:
                raise SyncCredentialInfrastructureError()
            return (await run_query(self._sb.rpc(name, args))).data
        except APIError as error:
            if error.code == "55000" and error.message == "sync_credential_claim_stale":
                raise SyncCredentialClaimStaleError() from None
            raise SyncCredentialInfrastructureError() from None
        except Exception:
            raise SyncCredentialInfrastructureError() from None

    async def _http(self, claim: SyncCredentialClaim, operation: str, version: str | None = None):
        try:
            if claim.claim_kind != "scheduled":
                raise SyncCredentialInfrastructureError()
            self._args(claim, version)
            url = getattr(self._config, "VERCEL_APP_URL", None)
            key = getattr(self._config, "INTERNAL_CREDENTIALS_API_KEY", None)
            if not isinstance(url, str) or not url or not isinstance(key, str) or not key:
                raise SyncCredentialInfrastructureError()
            body = claim.rpc_context()
            if version is not None:
                body["credential_version"] = version
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.post(
                    f"{url.rstrip('/')}/api/internal/credentials/{claim.credential_id}/{operation}",
                    headers={"Authorization": f"Bearer {key}", "X-Law-Firm-Id": claim.law_firm_id},
                    json=body,
                )
            data = response.json()
            if response.status_code == 409 and data == {"error": "sync_claim_stale"}:
                raise SyncCredentialClaimStaleError()
            if response.status_code != 200:
                raise SyncCredentialInfrastructureError()
            return data
        except SyncCredentialClaimStaleError:
            raise
        except Exception:
            raise SyncCredentialInfrastructureError() from None

    async def decrypt(self, claim: SyncCredentialClaim) -> SyncCredential:
        data = await self._http(claim, "decrypt")
        try:
            return SyncCredential.model_validate(data)
        except Exception:
            raise SyncCredentialInfrastructureError() from None

    async def invalidate(self, claim: SyncCredentialClaim, version: str) -> str:
        if version is None:
            raise SyncCredentialInfrastructureError()
        data = await self._http(claim, "invalidate", version)
        if data != {"ok": True, "status": "invalid", "outcome": "invalidated"} or data["ok"] is not True:
            raise SyncCredentialInfrastructureError()
        return "invalidated"

    async def check(self, claim: SyncCredentialClaim, version: str) -> None:
        if version is None:
            raise SyncCredentialInfrastructureError()
        data = await self._rpc("check_pjud_sync_credential_claim", self._args(claim, version))
        if data is False:
            raise SyncCredentialClaimStaleError()
        if data is not True:
            raise SyncCredentialInfrastructureError()

    async def finalize(self, claim: SyncCredentialClaim, version: str, result: dict) -> dict:
        if version is None:
            raise SyncCredentialInfrastructureError()
        data = await self._rpc("finalize_pjud_private_sync", {**self._args(claim, version), "p_result": result})
        if (not isinstance(data, dict) or set(data) != {"status", "new_movements"}
                or data["status"] not in ("published", "already_published", "stale")
                or type(data["new_movements"]) is not int or data["new_movements"] < 0
                or (data["status"] != "published" and data["new_movements"] != 0)):
            raise SyncCredentialInfrastructureError()
        return data

    async def failure(self, claim: SyncCredentialClaim, version: str | None, kind: str, code: str) -> str:
        if (kind, code) not in {("infra", "infra_unavailable"), ("ojv", "ojv_blocked"),
                              ("case", "case_not_found"), ("case", "parse_failed"), ("case", "identity_invalid")}:
            raise SyncCredentialInfrastructureError()
        if version is None and kind != "infra":
            raise SyncCredentialInfrastructureError()
        data = await self._rpc("record_pjud_private_sync_failure", {
            **self._args(claim, version), "p_failure_kind": kind, "p_error_code": code,
        })
        if not isinstance(data, dict) or set(data) != {"status"} or data["status"] not in ("recorded", "stale"):
            raise SyncCredentialInfrastructureError()
        return data["status"]
