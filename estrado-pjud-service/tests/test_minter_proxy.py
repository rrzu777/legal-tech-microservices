"""CookieMinter debe pasarle a Playwright las credenciales de proxy
SEPARADAS (server/username/password), nunca embebidas en la URL del
`server`. Embebidas rompe Chromium con net::ERR_INVALID_AUTH_CREDENTIALS
(verificado empíricamente en el VPS).
"""
from unittest.mock import AsyncMock, MagicMock, patch

from app.minter import CookieMinter

_DUMMY_PROXY = (
    "http://user123:pw_country-cl_session-abc_lifetime-1h@geo.iproyal.com:12321"
)


def _make_playwright_mock():
    """Construye un mock chain de async_playwright que soporta
    `async with async_playwright() as pw` y captura los kwargs de
    `pw.chromium.launch(**kwargs)`.

    Devuelve (async_playwright_mock, launch_mock) para poder inspeccionar
    las llamadas después.
    """
    pw_cookies = [
        {"name": "TSPD_101", "value": "abc", "domain": "oficinajudicialvirtual.pjud.cl"},
    ]

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value="Mozilla/5.0 Test UA")

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.cookies = AsyncMock(return_value=pw_cookies)

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    launch_mock = AsyncMock(return_value=browser)
    chromium = MagicMock()
    chromium.launch = launch_mock

    pw_instance = MagicMock()
    pw_instance.chromium = chromium

    async_playwright_cm = AsyncMock()
    async_playwright_cm.__aenter__ = AsyncMock(return_value=pw_instance)
    async_playwright_cm.__aexit__ = AsyncMock(return_value=False)

    async_playwright_factory = MagicMock(return_value=async_playwright_cm)

    return async_playwright_factory, launch_mock


async def test_mint_passes_separated_proxy_credentials_to_playwright():
    """Con un proxy con creds embebidas, `chromium.launch` debe recibir
    proxy={"server","username","password"} separados, NO la URL embebida.
    """
    async_playwright_factory, launch_mock = _make_playwright_mock()

    with patch("app.minter.async_playwright", async_playwright_factory):
        minter = CookieMinter("https://oficinajudicialvirtual.pjud.cl", proxy=_DUMMY_PROXY)
        result = await minter.mint()

    launch_mock.assert_awaited_once()
    _, kwargs = launch_mock.call_args
    assert kwargs["proxy"] == {
        "server": "http://geo.iproyal.com:12321",
        "username": "user123",
        "password": "pw_country-cl_session-abc_lifetime-1h",
    }
    assert result.cookies == {"TSPD_101": "abc"}
    assert result.user_agent == "Mozilla/5.0 Test UA"


async def test_mint_without_proxy_does_not_pass_proxy_kwarg():
    """Sin proxy configurado, `chromium.launch` NO debe recibir kwarg `proxy`."""
    async_playwright_factory, launch_mock = _make_playwright_mock()

    with patch("app.minter.async_playwright", async_playwright_factory):
        minter = CookieMinter("https://oficinajudicialvirtual.pjud.cl")
        result = await minter.mint()

    launch_mock.assert_awaited_once()
    _, kwargs = launch_mock.call_args
    assert "proxy" not in kwargs
    assert result.cookies == {"TSPD_101": "abc"}
