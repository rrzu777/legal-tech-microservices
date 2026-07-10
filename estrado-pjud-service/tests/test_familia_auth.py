# tests/test_familia_auth.py
import httpx
import pytest

from app.bandwidth import METER
from app.familia.auth import FamiliaAuthSession, FamiliaBlockedError, _detect_login_error


def test_familia_blocked_error_is_exception():
    assert issubclass(FamiliaBlockedError, Exception)


def test_detect_login_error_matches_rut_o_contrasena():
    # variante correcta y el typo real observado en el portal
    assert _detect_login_error("<p>RUT o contraseña incorrectos</p>") is True
    assert _detect_login_error("<p>rut o constraseña</p>") is True


def test_detect_login_error_negative():
    assert _detect_login_error("<html><body>Bienvenido</body></html>") is False


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
