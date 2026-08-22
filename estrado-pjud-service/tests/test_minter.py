import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError

from app.bandwidth import capture_proxy_usage
from app.cookie_scope import CookieRecord, playwright_cookie_records
from app.failure_kind import MintUnavailableError
from app.minter import CookieMinter, MintResult


class _FakeCDPSession:
    def __init__(self, *, detach_blocks: bool = False, fence_blocks: bool = False):
        self._callbacks = {}
        self._detach_blocks = detach_blocks
        self._fence_blocks = fence_blocks
        self._queued_events = []
        self.commands = []
        self.enabled = False
        self.detached = False
        self.detach_started = asyncio.Event()
        self.fence_started = asyncio.Event()

    def on(self, event, callback):
        self._callbacks[event] = callback

    async def send(self, command, _params=None):
        self.commands.append(command)
        if command == "Network.enable":
            self.enabled = True
        elif command == "Runtime.evaluate":
            self.fence_started.set()
            if self._fence_blocks:
                await asyncio.Event().wait()
            queued_events, self._queued_events = self._queued_events, []
            for event, payload in queued_events:
                self.emit(event, payload)

    def queue_event(self, event, payload):
        self._queued_events.append((event, payload))

    def emit(self, event, payload):
        callback = self._callbacks.get(event)
        if callback is not None:
            callback(payload)

    async def detach(self):
        self.detach_started.set()
        if self._detach_blocks:
            await asyncio.Event().wait()
        self.detached = True


class _FakePage:
    def __init__(
        self,
        context,
        *,
        navigation_fails: bool = False,
        queued_increments: tuple[int, ...] = (),
    ):
        self._context = context
        self._navigation_fails = navigation_fails
        self._queued_increments = queued_increments
        self._request_callback = None

    def on(self, event, callback):
        if event == "request":
            self._request_callback = callback

    async def goto(self, *_args, **_kwargs):
        assert self._context.cdp_created is True
        if self._context._cdp_error is None:
            assert self._context.cdp.enabled is True
        if self._request_callback is not None:
            self._request_callback(SimpleNamespace(post_data_buffer=None))
        if self._queued_increments:
            for increment in self._queued_increments:
                self._context.cdp.queue_event(
                    "Network.dataReceived",
                    {
                        "requestId": "queued-request-id-must-not-escape",
                        "encodedDataLength": increment,
                        "url": "https://queued-sensitive.invalid/private",
                    },
                )
            return
        self._context.cdp.emit(
            "Network.dataReceived",
            {
                "requestId": "opaque-request-id-must-not-escape",
                "encodedDataLength": 700,
                "url": "https://sensitive.invalid/private",
            },
        )
        self._context.cdp.emit(
            "Network.dataReceived",
            {
                "requestId": "opaque-request-id-must-not-escape",
                "encodedDataLength": 500,
            },
        )
        self._context.cdp.emit(
            "Network.dataReceived",
            {
                "requestId": "opaque-request-id-must-not-escape",
                "encodedDataLength": "not-a-number",
            },
        )
        if self._navigation_fails:
            self._context.cdp.emit(
                "Network.loadingFailed",
                {"requestId": "opaque-request-id-must-not-escape"},
            )
            raise PlaywrightError("raw navigation detail must not escape")
        self._context.cdp.emit(
            "Network.loadingFinished",
            {
                "requestId": "opaque-request-id-must-not-escape",
                "encodedDataLength": 1_200,
            },
        )

    async def wait_for_selector(self, *_args, **_kwargs):
        return None

    async def evaluate(self, expression):
        assert "performance" not in expression
        return "Mozilla/5.0 Test UA"


class _FakeContext:
    def __init__(
        self,
        *,
        navigation_fails: bool = False,
        detach_blocks: bool = False,
        fence_blocks: bool = False,
        queued_increments: tuple[int, ...] = (),
        cdp_error: Exception | None = None,
    ):
        self.cdp = _FakeCDPSession(
            detach_blocks=detach_blocks,
            fence_blocks=fence_blocks,
        )
        self.cdp_created = False
        self._cdp_error = cdp_error
        self.page = _FakePage(
            self,
            navigation_fails=navigation_fails,
            queued_increments=queued_increments,
        )

    async def new_page(self):
        return self.page

    async def new_cdp_session(self, page):
        assert page is self.page
        self.cdp_created = True
        if self._cdp_error is not None:
            raise self._cdp_error
        return self.cdp

    async def cookies(self):
        return [
            {
                "name": "TSPD_101",
                "value": "cookie-value-must-not-be-logged",
                "domain": "oficinajudicialvirtual.pjud.cl",
            },
        ]


class _FakeBrowser:
    def __init__(self, context):
        self._context = context
        self.closed = False
        self.closed_event = asyncio.Event()

    async def new_context(self):
        return self._context

    async def close(self):
        self.closed = True
        self.closed_event.set()


class _FakePlaywrightContext:
    def __init__(self, browser):
        self.chromium = SimpleNamespace(launch=AsyncMock(return_value=browser))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _playwright_factory(**context_kwargs):
    context = _FakeContext(**context_kwargs)
    browser = _FakeBrowser(context)
    return context, browser, lambda: _FakePlaywrightContext(browser)


def test_cookies_to_dict_extracts_name_value():
    pw_cookies = [
        {"name": "TSPD_101", "value": "abc", "domain": "oficinajudicialvirtual.pjud.cl"},
        {"name": "PHPSESSID", "value": "xyz", "domain": "oficinajudicialvirtual.pjud.cl"},
    ]
    result = playwright_cookie_records(pw_cookies)
    assert result == (
        CookieRecord("TSPD_101", "abc", "oficinajudicialvirtual.pjud.cl", "/"),
        CookieRecord("PHPSESSID", "xyz", "oficinajudicialvirtual.pjud.cl", "/"),
    )


def test_mint_result_holds_cookies_and_ua():
    record = CookieRecord("TSPD_101", "abc", "pjud.cl", "/")
    r = MintResult(cookies=(record,), user_agent="UA/1.0")
    assert r.cookies == (record,)
    assert r.user_agent == "UA/1.0"


@pytest.mark.asyncio
async def test_mint_counts_only_incremental_cdp_data_once_on_success(caplog):
    context, browser, factory = _playwright_factory()

    with patch("app.minter.async_playwright", factory):
        with capture_proxy_usage() as usage:
            result = await CookieMinter(
                "https://oficinajudicialvirtual.pjud.cl",
                proxy="http://proxy.invalid:1234",
            ).mint()

    assert result.user_agent == "Mozilla/5.0 Test UA"
    assert usage.request_count == 1
    assert usage.bytes_down == 1_200
    assert context.cdp.commands == ["Network.enable", "Runtime.evaluate"]
    assert context.cdp.detached is True
    assert browser.closed is True
    assert "opaque-request-id-must-not-escape" not in caplog.text
    assert "sensitive.invalid" not in caplog.text


@pytest.mark.asyncio
async def test_mint_counts_cdp_data_before_loading_failed_once(caplog):
    context, browser, factory = _playwright_factory(navigation_fails=True)

    with patch("app.minter.async_playwright", factory):
        with capture_proxy_usage() as usage:
            with pytest.raises(MintUnavailableError) as exc_info:
                await CookieMinter(
                    "https://oficinajudicialvirtual.pjud.cl",
                    proxy="http://proxy.invalid:1234",
                ).mint()

    assert exc_info.value.code == "navigation_failed"
    assert usage.request_count == 1
    assert usage.bytes_down == 1_200
    assert context.cdp.detached is True
    assert browser.closed is True
    assert "opaque-request-id-must-not-escape" not in caplog.text
    assert "raw navigation detail must not escape" not in caplog.text


@pytest.mark.asyncio
async def test_mint_fence_flushes_all_queued_cdp_data_before_scope_exit(caplog):
    context, browser, factory = _playwright_factory(
        queued_increments=(100, 200, 300, 400, 500),
    )

    with patch("app.minter.async_playwright", factory):
        with capture_proxy_usage() as usage:
            await CookieMinter("https://oficinajudicialvirtual.pjud.cl").mint()

    assert usage.bytes_down == 1_500
    assert context.cdp.commands == ["Network.enable", "Runtime.evaluate"]
    assert context.cdp.detached is True
    assert browser.closed is True
    assert "queued-request-id-must-not-escape" not in caplog.text
    assert "queued-sensitive.invalid" not in caplog.text


@pytest.mark.asyncio
async def test_mint_bounds_failed_cdp_fence_without_sensitive_logging(
    monkeypatch, caplog,
):
    context, browser, factory = _playwright_factory(
        fence_blocks=True,
        queued_increments=(100, 200, 300),
    )
    monkeypatch.setattr("app.minter._CLEANUP_TIMEOUT_S", 0.01)

    with patch("app.minter.async_playwright", factory):
        result = await asyncio.wait_for(
            CookieMinter("https://oficinajudicialvirtual.pjud.cl").mint(),
            timeout=0.2,
        )

    assert result.user_agent == "Mozilla/5.0 Test UA"
    assert context.cdp.commands == ["Network.enable", "Runtime.evaluate"]
    assert context.cdp.detached is True
    assert browser.closed is True
    assert "pjud_mint_cdp_fence_unavailable" in caplog.text
    assert "queued-request-id-must-not-escape" not in caplog.text
    assert "queued-sensitive.invalid" not in caplog.text


@pytest.mark.asyncio
async def test_mint_bounds_cdp_detach_before_browser_cleanup(monkeypatch):
    context, browser, factory = _playwright_factory(detach_blocks=True)
    monkeypatch.setattr("app.minter._CLEANUP_TIMEOUT_S", 0.01)

    with patch("app.minter.async_playwright", factory):
        result = await asyncio.wait_for(
            CookieMinter("https://oficinajudicialvirtual.pjud.cl").mint(),
            timeout=0.2,
        )

    assert result.user_agent == "Mozilla/5.0 Test UA"
    assert context.cdp.detach_started.is_set()
    assert context.cdp.detached is False
    assert browser.closed is True


@pytest.mark.asyncio
async def test_cancelled_mint_with_active_cdp_bounds_fence_and_cleanup(monkeypatch):
    context, browser, factory = _playwright_factory(
        fence_blocks=True,
        detach_blocks=True,
    )
    monkeypatch.setattr("app.minter._CLEANUP_TIMEOUT_S", 0.01)

    with patch("app.minter.async_playwright", factory):
        with capture_proxy_usage() as usage:
            task = asyncio.create_task(
                CookieMinter("https://oficinajudicialvirtual.pjud.cl").mint(),
            )
            await asyncio.wait_for(context.cdp.fence_started.wait(), timeout=0.1)
            task.cancel()
            await asyncio.wait_for(browser.closed_event.wait(), timeout=0.2)
            with pytest.raises(asyncio.CancelledError):
                await task

    assert usage.bytes_down == 1_200
    assert context.cdp.commands == ["Network.enable", "Runtime.evaluate"]
    assert context.cdp.detach_started.is_set()
    assert context.cdp.detached is False
    assert browser.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cdp_error",
    [
        PlaywrightError("raw CDP detail must not escape"),
        AttributeError("CDP method unavailable"),
    ],
)
async def test_mint_continues_when_cdp_is_unavailable(cdp_error):
    context, browser, factory = _playwright_factory(
        cdp_error=cdp_error,
    )

    with patch("app.minter.async_playwright", factory):
        result = await CookieMinter(
            "https://oficinajudicialvirtual.pjud.cl",
        ).mint()

    assert result.user_agent == "Mozilla/5.0 Test UA"
    assert context.cdp_created is True
    assert browser.closed is True
