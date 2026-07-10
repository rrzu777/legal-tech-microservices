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
