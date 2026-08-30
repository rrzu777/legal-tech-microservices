"""Synthetic closed claims shared by private sync boundary tests."""
from types import SimpleNamespace
from unittest.mock import MagicMock

VERSION = "2026-08-30T12:34:56.123456+00:00"
CLAIM = {
    "law_firm_id": "11111111-1111-1111-1111-111111111111",
    "credential_id": "22222222-2222-2222-2222-222222222222",
    "case_id": "33333333-3333-3333-3333-333333333333",
    "run_id": "44444444-4444-4444-4444-444444444444",
    "claim_kind": "scheduled", "claim_owner": "worker-test",
    "claim_token": "55555555-5555-5555-5555-555555555555",
    "case_binding_version": 7,
}
WEB_CLAIM = {**CLAIM, "claim_kind": "manual", "claim_owner": CLAIM["run_id"],
             "claim_token": "sync:synthetic-secret-token"}
PAYLOAD = {"rut": "11111111-1", "password": "synthetic-password", "auth_type": "clave_pj",
           "cases": [{"rit": "100", "year": "2024"}],
           "sync_claim": WEB_CLAIM, "credential_version": VERSION}
CREDENTIAL = {"rut": "11111111-1", "password": "synthetic-password",
              "password_type": "clave_poder_judicial", "credential_version": VERSION}


def rpc_client(outcomes):
    """Fake only the synchronous PostgREST execution boundary."""
    client = MagicMock()
    pending = iter(outcomes)
    def rpc(name, args):
        result = next(pending)
        def execute():
            if isinstance(result, Exception):
                raise result
            return SimpleNamespace(data=result)
        return SimpleNamespace(execute=execute)
    client.rpc.side_effect = rpc
    client.from_.side_effect = AssertionError("unfenced direct write")
    return client
