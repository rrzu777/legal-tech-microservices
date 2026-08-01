# tests/test_familia_models.py
from app.familia.models import FamiliaSyncRequest, FamiliaSyncResponse


def test_blocked_is_a_valid_error_code():
    resp = FamiliaSyncResponse(
        ok=False, casos=[], error_code="blocked", error="reintentá luego"
    )
    assert resp.error_code == "blocked"


def test_default_auth_type_is_clave_pj():
    req = FamiliaSyncRequest(rut="11111111-1", password="x")
    assert req.auth_type == "clave_pj"


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
    }
