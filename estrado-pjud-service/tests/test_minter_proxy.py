"""CookieMinter debe pasarle a Playwright las credenciales de proxy
SEPARADAS (server/username/password), nunca embebidas en la URL del
`server`. Embebidas rompe Chromium con net::ERR_INVALID_AUTH_CREDENTIALS
(verificado empíricamente en el VPS).
"""
from copy import copy
from http.cookiejar import CookieJar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bandwidth import capture_proxy_usage
from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.minter import CookieMinter, cookies_to_dict

_DUMMY_PROXY = (
    "http://user123:pw_country-cl_session-abc_lifetime-1h@geo.iproyal.com:12321"
)


class _DuplicateCookieJar(CookieJar):
    """CookieJar iterable for the impossible-to-store same-scope value conflict."""

    def __init__(self, records):
        self._records = records

    def __iter__(self):
        return iter(self._records)


def _make_playwright_mock(*, cookies=None, user_agent="Mozilla/5.0 Test UA"):
    """Construye un mock chain de async_playwright que soporta
    `async with async_playwright() as pw` y captura los kwargs de
    `pw.chromium.launch(**kwargs)`.

    Devuelve (async_playwright_mock, launch_mock) para poder inspeccionar
    las llamadas después.
    """
    pw_cookies = cookies or [
        {"name": "TSPD_101", "value": "abc", "domain": "oficinajudicialvirtual.pjud.cl"},
    ]

    page = AsyncMock()
    page.on = MagicMock()
    page.evaluate = AsyncMock(return_value=user_agent)

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

    return async_playwright_factory, launch_mock, page


async def test_mint_accepts_real_form_with_renamed_f5_cookies(caplog):
    """Changing the F5 cookie suffix must not reject a form-ready PJUD session."""
    sentinel_ua = "UA-SENTINEL-DO-NOT-LOG"
    sentinel_cookie_name = "TSa2ac8a0a027"
    sentinel_cookie_value = "f5-value-sentinel"
    factory, _, _ = _make_playwright_mock(
        cookies=[
            {"name": "PHPSESSID", "value": "php", "domain": "oficinajudicialvirtual.pjud.cl"},
            {"name": "TS01262d1d", "value": "f5-a", "domain": "oficinajudicialvirtual.pjud.cl"},
            {
                "name": sentinel_cookie_name,
                "value": sentinel_cookie_value,
                "domain": "oficinajudicialvirtual.pjud.cl",
            },
        ],
        user_agent=sentinel_ua,
    )

    with patch("app.minter.async_playwright", factory):
        with caplog.at_level("INFO", logger="app.minter"):
            result = await CookieMinter("https://oficinajudicialvirtual.pjud.cl").mint()

    assert set(result.cookies) == {"PHPSESSID", "TS01262d1d", sentinel_cookie_name}
    log_output = caplog.text
    assert sentinel_cookie_name not in log_output
    assert sentinel_cookie_value not in log_output
    assert sentinel_ua not in log_output


@pytest.mark.parametrize("changed_field", ["value", "domain", "path"])
async def test_mint_rejects_ambiguous_scoped_cookie_without_disclosing_it(
    caplog, changed_field,
):
    """A browser jar with conflicting scoped values cannot be flattened safely."""
    sentinel_name = "cookie-name-sentinel"
    sentinel_value = "cookie-value-sentinel"
    sentinel_domain = "cookie-domain-sentinel.test"
    sentinel_path = "/cookie-path-sentinel"
    first_cookie = {
        "name": sentinel_name,
        "value": sentinel_value,
        "domain": sentinel_domain,
        "path": sentinel_path,
    }
    second_cookie = dict(first_cookie)
    second_cookie[changed_field] = f"other-{changed_field}-sentinel"
    factory, _, _ = _make_playwright_mock(cookies=[first_cookie, second_cookie])

    with patch("app.minter.async_playwright", factory):
        with caplog.at_level("INFO", logger="app.minter"):
            with pytest.raises(ValueError, match="ambiguous_cookie_scope") as exc_info:
                await CookieMinter("https://oficinajudicialvirtual.pjud.cl").mint()

    output = f"{exc_info.value}\n{caplog.text}"
    for sentinel in (*first_cookie.values(), *second_cookie.values()):
        assert sentinel not in output


def test_cookie_conversion_allows_exact_scoped_duplicates_and_distinct_names():
    """Identical browser records and independently named cookies are unambiguous."""
    result = cookies_to_dict([
        {"name": "PHPSESSID", "value": "php", "domain": "ojv.test", "path": "/"},
        {"name": "PHPSESSID", "value": "php", "domain": "ojv.test", "path": "/"},
        {"name": "TS-current", "value": "f5", "domain": "ojv.test", "path": "/"},
    ])

    assert result == {"PHPSESSID": "php", "TS-current": "f5"}


async def test_snapshot_accepts_identical_scoped_duplicates_and_distinct_names():
    """Identical jar records can flatten safely alongside independently named cookies."""
    adapter = OJVHttpAdapter(Settings(API_KEY="t", _env_file=None))
    adapter.cookies.set("PHPSESSID", "php", domain="ojv.test", path="/")
    adapter.cookies.set("TS-current", "f5", domain="ojv.test", path="/")
    records = list(adapter.cookies.jar)
    php_session = next(cookie for cookie in records if cookie.name == "PHPSESSID")
    adapter.cookies.jar = _DuplicateCookieJar([php_session, copy(php_session), *records])

    try:
        assert adapter.snapshot_cookies() == {"PHPSESSID": "php", "TS-current": "f5"}
    finally:
        await adapter.close()


@pytest.mark.parametrize("changed_field", ["value", "domain", "path"])
async def test_snapshot_rejects_ambiguous_httpx_cookie_scopes_without_disclosing_them(
    changed_field,
):
    """A real httpx jar must fail closed before a name-only snapshot loses scope."""
    sentinel_name = "jar-name-sentinel"
    sentinel_value = "jar-value-sentinel"
    sentinel_domain = "jar-domain-sentinel.test"
    sentinel_path = "/jar-path-sentinel"
    first_cookie = {
        "value": sentinel_value,
        "domain": sentinel_domain,
        "path": sentinel_path,
    }
    second_cookie = dict(first_cookie)
    second_cookie[changed_field] = f"jar-other-{changed_field}-sentinel"
    adapter = OJVHttpAdapter(Settings(API_KEY="t", _env_file=None))
    adapter.cookies.set(sentinel_name, **first_cookie)
    if changed_field == "value":
        first_record = next(iter(adapter.cookies.jar))
        second_record = copy(first_record)
        second_record.value = second_cookie["value"]
        adapter.cookies.jar = _DuplicateCookieJar([first_record, second_record])
    else:
        adapter.cookies.set(sentinel_name, **second_cookie)

    try:
        with pytest.raises(ValueError, match="ambiguous_cookie_scope") as exc_info:
            adapter.snapshot_cookies()
    finally:
        await adapter.close()

    output = str(exc_info.value)
    for sentinel in (sentinel_name, *first_cookie.values(), *second_cookie.values()):
        assert sentinel not in output


async def test_mint_passes_separated_proxy_credentials_to_playwright():
    """Con un proxy con creds embebidas, `chromium.launch` debe recibir
    proxy={"server","username","password"} separados, NO la URL embebida.
    """
    async_playwright_factory, launch_mock, _ = _make_playwright_mock()

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
    async_playwright_factory, launch_mock, _ = _make_playwright_mock()

    with patch("app.minter.async_playwright", async_playwright_factory):
        minter = CookieMinter("https://oficinajudicialvirtual.pjud.cl")
        result = await minter.mint()

    launch_mock.assert_awaited_once()
    _, kwargs = launch_mock.call_args
    assert "proxy" not in kwargs
    assert result.cookies == {"TSPD_101": "abc"}


async def test_mint_attributes_chromium_transfer_sizes_to_active_operation():
    async_playwright_factory, _, page = _make_playwright_mock()
    page.evaluate.side_effect = [
        "Mozilla/5.0 Test UA",
        [{"transferSize": 700}, {"transferSize": 500}],
    ]

    with patch("app.minter.async_playwright", async_playwright_factory):
        with capture_proxy_usage() as usage:
            await CookieMinter(
                "https://oficinajudicialvirtual.pjud.cl", proxy=_DUMMY_PROXY,
            ).mint()

    assert usage.request_count == 2
    assert usage.bytes_down == 1_200


async def test_failed_mint_still_attributes_paid_transfer_sizes():
    async_playwright_factory, _, page = _make_playwright_mock()
    page.wait_for_selector.side_effect = TimeoutError("challenge timeout")
    page.evaluate.return_value = [{"transferSize": 1_500}]

    with patch("app.minter.async_playwright", async_playwright_factory):
        with capture_proxy_usage() as usage:
            with pytest.raises(TimeoutError, match="challenge timeout"):
                await CookieMinter(
                    "https://oficinajudicialvirtual.pjud.cl", proxy=_DUMMY_PROXY,
                ).mint()

    assert usage.request_count == 1
    assert usage.bytes_down == 1_500


async def test_navigation_failure_still_records_that_proxy_was_contacted():
    async_playwright_factory, _, page = _make_playwright_mock()
    request = MagicMock(post_data_buffer=None)
    page.on.side_effect = lambda event, callback: callback(request)
    page.goto.side_effect = TimeoutError("navigation timeout")
    page.evaluate.side_effect = RuntimeError("page unavailable")

    with patch("app.minter.async_playwright", async_playwright_factory):
        with capture_proxy_usage() as usage:
            with pytest.raises(TimeoutError, match="navigation timeout"):
                await CookieMinter(
                    "https://oficinajudicialvirtual.pjud.cl", proxy=_DUMMY_PROXY,
                ).mint()

    assert usage.request_count == 1
