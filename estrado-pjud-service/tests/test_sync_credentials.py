import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import ValidationError

from tests.sync_claim_helpers import CLAIM, CREDENTIAL, VERSION, WEB_CLAIM, rpc_client


def boundary():
    from worker import sync_credentials
    return sync_credentials


@pytest.mark.parametrize("patch", [{"extra": "x"}, {"run_id": ""}, {"case_binding_version": True},
    {"case_binding_version": -1}, {"case_binding_version": 9007199254740992},
    {"claim_owner": " worker"}, {"claim_owner": "sync:bad"}, {"claim_token": "bad"}])
def test_closed_claim_rejects_unbound_context(patch):
    with pytest.raises(ValidationError):
        boundary().SyncCredentialClaim.model_validate({**CLAIM, **patch})


def test_claim_and_snapshot_hide_secrets_and_keep_exact_revision():
    mod = boundary()
    claim = mod.SyncCredentialClaim.model_validate(WEB_CLAIM)
    cred = mod.SyncCredential.model_validate(CREDENTIAL)
    assert claim.rpc_context() == WEB_CLAIM
    assert cred.credential_version == VERSION
    assert WEB_CLAIM["claim_token"] not in repr(claim)
    assert CREDENTIAL["password"] not in repr(cred)


@pytest.mark.parametrize("identifier", ["abcdef12-abcd-abcd-abcd-abcdef123456", "ABCDEF12-ABCD-ABCD-ABCD-ABCDEF123456"])
@pytest.mark.parametrize("field", ["law_firm_id", "credential_id", "case_id", "run_id", "claim_token"])
def test_uuid_validation_accepts_both_cases_without_reserializing(field, identifier):
    claim = boundary().SyncCredentialClaim.model_validate({**CLAIM, field: identifier})
    assert claim.rpc_context()[field] == identifier


@pytest.mark.asyncio
async def test_http_post_body_and_tenant_are_exact(monkeypatch):
    mod = boundary()
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=CREDENTIAL if request.url.path.endswith("decrypt")
                              else {"ok": True, "status": "invalid", "outcome": "invalidated"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    client = mod.SyncCredentialClient(rpc_client([]), SimpleNamespace(VERCEL_APP_URL="https://app.test", INTERNAL_CREDENTIALS_API_KEY="k"))
    claim = mod.SyncCredentialClaim.model_validate(CLAIM)
    cred = await client.decrypt(claim)
    assert await client.invalidate(claim, cred.credential_version) == "invalidated"
    assert [r.method for r in seen] == ["POST", "POST"]
    assert json.loads(seen[0].content) == CLAIM
    assert json.loads(seen[1].content) == {**CLAIM, "credential_version": VERSION}
    for request in seen:
        assert request.headers["Authorization"] == "Bearer k"
        assert request.headers["X-Law-Firm-Id"] == CLAIM["law_firm_id"]
        assert CLAIM["claim_token"] not in str(request.url)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 409, 422, 429, 500, 503])
async def test_http_errors_are_infrastructure_not_bad_credentials(monkeypatch, status):
    mod = boundary()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"error": "unknown"})), **kw))
    client = mod.SyncCredentialClient(None, SimpleNamespace(VERCEL_APP_URL="https://app.test", INTERNAL_CREDENTIALS_API_KEY="k"))
    with pytest.raises(mod.SyncCredentialInfrastructureError, match="^infra_unavailable$"):
        await client.decrypt(mod.SyncCredentialClaim.model_validate(CLAIM))


@pytest.mark.asyncio
async def test_only_exact_http_stale_is_stale(monkeypatch):
    mod = boundary()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(409, json={"error": "sync_claim_stale"})), **kw))
    client = mod.SyncCredentialClient(None, SimpleNamespace(VERCEL_APP_URL="https://app.test", INTERNAL_CREDENTIALS_API_KEY="k"))
    with pytest.raises(mod.SyncCredentialClaimStaleError):
        await client.decrypt(mod.SyncCredentialClaim.model_validate(CLAIM))


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message,stale", [("55000", "sync_credential_claim_stale", True),
    ("55000", "audit failed SECRET", False), ("55P03", "lock SECRET", False),
    ("42501", "permission SECRET", False)])
async def test_sql_error_classification_never_retains_raw_error(code, message, stale):
    from postgrest.exceptions import APIError
    mod = boundary()
    client = mod.SyncCredentialClient(rpc_client([APIError({"code": code, "message": message, "details": "SECRET", "hint": "SECRET"})]))
    error_type = mod.SyncCredentialClaimStaleError if stale else mod.SyncCredentialInfrastructureError
    with pytest.raises(error_type) as err:
        await client.check(mod.SyncCredentialClaim.model_validate(CLAIM), VERSION)
    assert "SECRET" not in repr(err.value)


@pytest.mark.asyncio
async def test_rpc_fences_and_closed_responses():
    mod = boundary()
    sb = rpc_client([True, {"status": "published", "new_movements": 1}, {"status": "recorded"}])
    client = mod.SyncCredentialClient(sb)
    claim = mod.SyncCredentialClaim.model_validate(CLAIM)
    await client.check(claim, VERSION)
    result = {"estado": "Tramitación", "materia": "", "tribunal": "Tribunal", "caratulado": "", "fecha_ingreso": None}
    assert await client.finalize(claim, VERSION, result) == {"status": "published", "new_movements": 1}
    assert await client.failure(claim, None, "infra", "infra_unavailable") == "recorded"
    assert sb.rpc.call_args_list[0].args == ("check_pjud_sync_credential_claim", {"p_context": CLAIM, "p_credential_version": VERSION})
    assert sb.rpc.call_args_list[1].args == ("finalize_pjud_private_sync", {"p_context": CLAIM, "p_credential_version": VERSION, "p_result": result})
    assert sb.rpc.call_args_list[2].args == ("record_pjud_private_sync_failure", {"p_context": CLAIM, "p_credential_version": None, "p_failure_kind": "infra", "p_error_code": "infra_unavailable"})


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"ok": 1, "status": "invalid", "outcome": "invalidated"},
    {"ok": True, "status": "invalid", "outcome": "invalidated", "extra": "bad"}])
async def test_invalidation_rejects_malformed_success(monkeypatch, data):
    mod = boundary()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)), **kw))
    client = mod.SyncCredentialClient(None, SimpleNamespace(VERCEL_APP_URL="https://app.test", INTERNAL_CREDENTIALS_API_KEY="k"))
    with pytest.raises(mod.SyncCredentialInfrastructureError):
        await client.invalidate(mod.SyncCredentialClaim.model_validate(CLAIM), VERSION)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,data", [("check", 1), ("check", None),
    ("finalize", {"status": "already_published", "new_movements": 1}),
    ("finalize", {"status": "published", "new_movements": True}),
    ("failure", {"status": "recorded", "extra": "bad"})])
async def test_malformed_rpc_results_never_authorize(operation, data):
    mod = boundary()
    client = mod.SyncCredentialClient(rpc_client([data]))
    claim = mod.SyncCredentialClaim.model_validate(CLAIM)
    with pytest.raises(mod.SyncCredentialInfrastructureError):
        if operation == "check":
            await client.check(claim, VERSION)
        elif operation == "finalize":
            await client.finalize(claim, VERSION, {"estado": "ok"})
        else:
            await client.failure(claim, VERSION, "infra", "infra_unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [{**CREDENTIAL, "credential_version": "2026-08-30"},
    {**CREDENTIAL, "password_type": "clave_unica"}, {**CREDENTIAL, "extra": "bad"},
    {**CREDENTIAL, "password": ""}])
async def test_malformed_snapshot_is_infrastructure(monkeypatch, snapshot):
    mod = boundary()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)), **kw))
    client = mod.SyncCredentialClient(None, SimpleNamespace(VERCEL_APP_URL="https://app.test", INTERNAL_CREDENTIALS_API_KEY="k"))
    with pytest.raises(mod.SyncCredentialInfrastructureError):
        await client.decrypt(mod.SyncCredentialClaim.model_validate(CLAIM))
