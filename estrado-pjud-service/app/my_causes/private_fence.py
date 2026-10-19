"""Candidate/credential authority, rechecked before every provider request."""
from __future__ import annotations

from datetime import datetime

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.my_causes.models import ImportCandidate
from app.my_causes.private_models import AuthenticatedImportError, AuthenticatedImportRequest
from app.runtime_fence import runtime_generation_headers
from app.supabase_executor import execute_supabase_query


class ImportDetailFence:
    def __init__(self, request: AuthenticatedImportRequest, state):
        self.request = request
        self.state = state
        self.candidate: ImportCandidate | None = None

    async def check(self) -> ImportCandidate:
        await self.state.pjud_runtime_fence.require()
        req = self.request
        try:
            response = await execute_supabase_query(self.state.proxy_supabase.rpc(
                "check_pjud_authenticated_import_stage", {
                    "p_candidate_id": str(req.candidate_id),
                    "p_claim_token": str(req.claim_token),
                    "p_worker_id": req.worker_id,
                    "p_law_firm_id": str(req.law_firm_id),
                    "p_credential_id": str(req.credential_id),
                    "p_expected_credential_updated_at": req.credential_binding_version.isoformat(),
                },
            ))
            value = response.data
            if not isinstance(value, dict) or value.get("status") != "valid":
                raise ValueError("stale")
            revision = datetime.fromisoformat(value["credential_binding_version"].replace("Z", "+00:00"))
            candidate = ImportCandidate.model_validate(value["candidate"])
            if revision != req.credential_binding_version or candidate.matter not in {"civil", "apelaciones"}:
                raise ValueError("invalid_binding")
            if self.candidate is not None and candidate != self.candidate:
                raise ValueError("candidate_changed")
        except Exception:
            raise AuthenticatedImportError("private_fence_unavailable") from None
        self.candidate = candidate
        return candidate


async def fetch_import_credential(
    req: AuthenticatedImportRequest, settings: Settings,
) -> tuple[SecretStr, SecretStr]:
    """Decrypt only through the web's existing candidate-claim boundary."""
    if not settings.VERCEL_APP_URL.startswith("https://") or not settings.INTERNAL_CREDENTIALS_API_KEY:
        raise AuthenticatedImportError("private_fence_unavailable")
    credential = None
    response = None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(
                settings.VERCEL_APP_URL.rstrip("/")
                + f"/api/internal/pjud-import/credentials/{req.credential_id}/decrypt",
                headers={
                    "Authorization": f"Bearer {settings.INTERNAL_CREDENTIALS_API_KEY}",
                    "X-Law-Firm-Id": str(req.law_firm_id),
                    "X-Pjud-Import-Candidate-Id": str(req.candidate_id),
                    "X-Pjud-Import-Claim-Token": str(req.claim_token),
                    "X-Pjud-Import-Worker-Id": req.worker_id,
                    **runtime_generation_headers(settings.PJUD_RUNTIME_GENERATION),
                },
            )
            if response.status_code != 200:
                raise ValueError("credential_unavailable")
            credential = response.json()
        if not isinstance(credential, dict) or set(credential) != {"rut", "password", "password_type", "binding_version"}:
            raise ValueError("credential_contract_changed")
        if (credential["password_type"] != "clave_poder_judicial"
                or datetime.fromisoformat(credential["binding_version"].replace("Z", "+00:00")) != req.credential_binding_version
                or not all(isinstance(credential[k], str) and credential[k] for k in ("rut", "password"))):
            raise ValueError("credential_binding_changed")
        return SecretStr(credential["rut"]), SecretStr(credential["password"])
    except Exception:
        pass
    finally:
        if isinstance(credential, dict):
            credential.clear()
        response = None

    # Raise outside the handler so no transport exception retains its request.
    raise AuthenticatedImportError("private_fence_unavailable")
