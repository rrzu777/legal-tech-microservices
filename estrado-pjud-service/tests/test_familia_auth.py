# tests/test_familia_auth.py
import httpx
import pytest

from app.bandwidth import METER
from app.familia.auth import (
    FamiliaAuthSession,
    FamiliaBlockedError,
    InvalidCredentialsError,
    OjvSession,
    SessionError,
    _detect_login_error,
    _rut_parts,
)


def test_familia_blocked_error_is_exception():
    assert issubclass(FamiliaBlockedError, Exception)


def test_familia_session_preserves_public_type_while_reusing_ojv_primitives():
    assert issubclass(FamiliaAuthSession, OjvSession)


def test_detect_login_error_matches_rut_o_contrasena():
    # variante correcta y el typo real observado en el portal
    assert _detect_login_error("<p>RUT o contraseña incorrectos</p>") is True
    assert _detect_login_error("<p>rut o constraseña</p>") is True


def test_detect_login_error_negative():
    assert _detect_login_error("<html><body>Bienvenido</body></html>") is False


def test_rut_parts_preserves_legacy_bare_eight_digit_body():
    assert _rut_parts("12345678") == ("12345678", "")
    assert _rut_parts("1.234.567-K") == ("1234567", "K")


async def test_constructor_wires_proxy_cookies_and_ua():
    s = FamiliaAuthSession(
        proxy_url=None, cookies={"TSPD_101": "abc"}, user_agent="UA/test"
    )
    assert s._client.headers["User-Agent"] == "UA/test"
    assert s._client.cookies.get("TSPD_101") == "abc"
    await s.close()


async def test_login_rejects_clave_unica():
    s = FamiliaAuthSession(proxy_url=None, cookies=None, user_agent=None)
    with pytest.raises(ValueError):
        await s.login("11111111-1", "x", "clave_unica")
    await s.close()


async def test_search_familia_counts_bandwidth():
    METER.reset()

    def handler(request):
        return httpx.Response(200, text="<html><table></table></html>")

    s = FamiliaAuthSession(proxy_url=None, cookies=None, user_agent=None, rate_limit_s=0)
    # Reemplazar el cliente real por uno con transporte mockeado (sin red).
    await s._client.aclose()
    s._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    await s.search_familia(rut="11111111-1")
    assert METER.total_bytes > 0
    await s.close()


from app.familia.auth import FamiliaBlockedError as _FBE  # alias para claridad


async def test_search_raises_blocked_on_f5_challenge():
    # 'bobcmn' es el marcador de challenge F5 que detect_blocked reconoce.
    def handler(request):
        return httpx.Response(200, text="<html>window.bobcmn = 1</html>")

    s = FamiliaAuthSession(proxy_url=None, cookies=None, user_agent=None, rate_limit_s=0)
    await s._client.aclose()
    s._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    with pytest.raises(_FBE):
        await s.search_familia(rut="11111111-1")
    await s.close()


async def test_login_clave_pj_raises_blocked_on_f5_challenge():
    def handler(request):
        return httpx.Response(200, text="<html>bobcmn challenge</html>")

    s = FamiliaAuthSession(proxy_url=None, cookies=None, user_agent=None, rate_limit_s=0)
    await s._client.aclose()
    s._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    with pytest.raises(_FBE):
        await s.login("11111111-1", "x", "clave_pj")
    await s.close()


async def test_successful_login_retains_redacted_identity_only_in_memory():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("login_pjud.html"):
            return httpx.Response(200, text="login", request=request)
        if request.url.path.endswith("login_pjud"):
            return httpx.Response(
                302,
                headers={"Location": "https://oficinajudicialvirtual.pjud.cl/indexN.php"},
                request=request,
            )
        return httpx.Response(200, text="<html>Bienvenido</html>", request=request)

    s = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    await s.login("11.111.111-1", "synthetic-password", "clave_pj")

    assert s.authenticated_form_identity() == ("11111111", "1")
    representation = repr(vars(s))
    assert "11111111" not in representation
    assert "synthetic-password" not in representation

    await s.close()
    with pytest.raises(SessionError, match="authenticated identity unavailable"):
        s.authenticated_form_identity()


async def test_rejected_login_never_retains_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>RUT o contraseña incorrectos</html>",
            request=request,
        )

    s = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    with pytest.raises(InvalidCredentialsError):
        await s.login("11.111.111-1", "synthetic-password", "clave_pj")
    with pytest.raises(SessionError, match="authenticated identity unavailable"):
        s.authenticated_form_identity()
    await s.close()


async def test_bare_eight_digit_login_stays_compatible_but_cannot_seed_discovery():
    posted_rut: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("login_pjud.html"):
            return httpx.Response(200, text="login", request=request)
        if request.url.path.endswith("login_pjud"):
            posted_rut.append(dict(httpx.QueryParams(request.content.decode()))["rutPjud"])
            return httpx.Response(
                302,
                headers={"Location": "https://oficinajudicialvirtual.pjud.cl/indexN.php"},
                request=request,
            )
        return httpx.Response(200, text="<html>Bienvenido</html>", request=request)

    s = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    await s.login("12345678", "synthetic-password", "clave_pj")

    assert posted_rut == ["12345678"]
    with pytest.raises(SessionError, match="authenticated identity unavailable"):
        s.authenticated_form_identity()
    await s.close()
