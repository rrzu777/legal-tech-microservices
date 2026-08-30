# tests/test_familia_models.py
from app.familia.models import FamiliaSyncRequest, FamiliaSyncResponse
import pytest
from pydantic import ValidationError
from tests.sync_claim_helpers import PAYLOAD


@pytest.mark.parametrize("patch", [{"sync_claim": None}, {"credential_version": None},
    {"cases": []}, {"cases": [{"rit": "1", "year": "2024"}] * 2},
    {"auth_type": "clave_unica"}, {"extra": "forbidden"}])
def test_sync_rejects_legacy_unfenced_or_batch_requests(patch):
    with pytest.raises(ValidationError):
        FamiliaSyncRequest.model_validate({**PAYLOAD, **patch})


def test_blocked_is_a_valid_error_code():
    resp = FamiliaSyncResponse(
        ok=False, casos=[], error_code="blocked", error="reintentá luego"
    )
    assert resp.error_code == "blocked"


def test_default_auth_type_is_clave_pj():
    req = FamiliaSyncRequest.model_validate({**PAYLOAD, "password": "x"})
    assert req.auth_type == "clave_pj"


def test_secret_wrappers_preserve_the_existing_request_json_schema():
    properties = FamiliaSyncRequest.model_json_schema()["properties"]

    assert properties["rut"] == {"title": "Rut", "type": "string"}
    assert properties["password"] == {"title": "Password", "type": "string"}


def test_request_repr_and_dump_redact_rut_and_password():
    request = FamiliaSyncRequest.model_validate({**PAYLOAD, "rut": "11.111.111-1"})

    rendered = f"{request!r} {request.model_dump()!r}"
    assert "11.111.111-1" not in rendered
    assert "synthetic-password" not in rendered


def test_el_conjunto_de_error_code_esta_fijado():
    """⚠️ Contrato cross-repo: si esto falla, `classifyFamiliaFailure` en el repo
    de la app (`apps/web/src/lib/pjud/sync-error-patch.ts`) tiene que aprender el
    codigo nuevo antes de que se despliegue este servicio.

    Sin el test, agregar un codigo aca no rompe nada visible: la app lo manda al
    default y trata la falla como de la causa. Un codigo que en realidad
    significara "se cayo nuestro lado" terminaria sumando fallas y, a las 10,
    suspendiendo la causa — que es exactamente el defecto que esta PR arregla,
    reintroducido por la puerta de atras.
    """
    from typing import get_args

    from app.familia.models import FamiliaErrorCode

    assert set(get_args(FamiliaErrorCode)) == {
        "invalid_credentials",
        "session_error",
        "no_cases",
        "parse_error",
        "blocked",
        "sync_claim_stale",
    }
