from __future__ import annotations

import asyncio
import httpx
import time
import pytest
from pydantic import SecretStr
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from app.bandwidth import capture_proxy_usage
from app.cookie_scope import CookieRecord
from app.ojv.browser_login import BrowserLoginResult, _resolve_post_submit, login_official_ojv
from app.ojv.errors import FamiliaBlockedError, InvalidCredentialsError, OjvTimeoutError, OjvUpstreamChangedError
from app.ojv.session import OjvSession


async def test_official_login_result_replaces_borrowed_public_bundle_for_listing() -> None:
    calls: list[tuple[str, str | None, str]] = []

    async def official_login(
        rut: SecretStr,
        password: SecretStr,
        *,
        proxy_url: str | None,
        user_agent: str,
    ) -> BrowserLoginResult:
        calls.append((rut.get_secret_value(), proxy_url, user_agent))
        assert password.get_secret_value() == "synthetic-password-never-log"
        return BrowserLoginResult((
            CookieRecord(
                name="AUTH", value="authenticated-cookie", domain="oficinajudicialvirtual.pjud.cl",
                secure=True,
            ),
        ), user_agent=user_agent)

    seen_cookies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie", ""))
        return httpx.Response(200, text="ok", request=request)

    session = OjvSession(
        proxy_url=None,
        cookies={"F5": "borrowed-public-cookie"},
        user_agent="official-test-agent",
        rate_limit_s=0,
        transport=httpx.MockTransport(handler),
        browser_login=official_login,
    )
    await session.login(SecretStr("11.111.111-1"), SecretStr("synthetic-password-never-log"))
    await session._get("https://oficinajudicialvirtual.pjud.cl/misCausas/civil")

    assert calls == [("11.111.111-1", None, "official-test-agent")]
    assert seen_cookies == ["AUTH=authenticated-cookie"]
    assert session.authenticated_form_identity() == ("11111111", "1")
    await session.close()


def test_browser_login_result_repr_redacts_cookie_values() -> None:
    result = BrowserLoginResult((CookieRecord(
        name="AUTH", value="cookie-secret-must-not-render", domain="oficinajudicialvirtual.pjud.cl",
    ),), user_agent="ua")
    assert "cookie-secret-must-not-render" not in repr(result)


class _Counted:
    def __init__(self, count: int = 1, placeholder: str | None = None) -> None:
        self._count = count
        self._placeholder = placeholder
        self.filled: list[str] = []

    async def count(self) -> int:
        return self._count

    def filter(self, **_kwargs: object) -> "_Counted":
        return self

    async def wait_for(self, **_kwargs: object) -> None:
        return None

    async def get_attribute(self, name: str) -> str | None:
        return self._placeholder if name == "placeholder" else None

    async def fill(self, value: str, **_kwargs: object) -> None:
        self.filled.append(value)


class _Form(_Counted):
    def __init__(self, page: "_Page") -> None:
        super().__init__()
        self.page = page
        self.rut = _Counted(placeholder="Ingrese su Rut sin dígito verificador, Ej: 12345678")
        self.password = _Counted()

    def locator(self, selector: str) -> _Counted:
        return {"input[type=text]": self.rut, "input[type=password]": self.password}[selector]

    def get_by_role(self, role: str, *, name: str, exact: bool, include_hidden: bool = False) -> "_Action":
        assert (role, name, exact) == ("button", "Ingresar", True)
        return _Action(self.page, "submit")


class _Action(_Counted):
    def __init__(self, page: "_Page", action: str) -> None:
        super().__init__()
        self.page = page
        self.action = action

    async def click(self, **_kwargs: object) -> None:
        self.page.actions.append(self.action)
        if self.action == "submit":
            self.page.url = "https://oficinajudicialvirtual.pjud.cl/indexN.php"


class _Modal(_Counted):
    def __init__(self, form: _Form) -> None:
        super().__init__()
        self.form = form

    def locator(self, selector: str) -> _Form:
        assert selector == "#fSGN"
        return self.form


class _Page:
    def __init__(self) -> None:
        self.url = ""
        self.actions: list[str] = []
        self.form = _Form(self)
        self.modal = _Modal(self.form)
        self.events: list[str] = []
        self.callbacks: dict[str, object] = {}

    def on(self, event: str, _callback: object) -> None:
        self.events.append(event)
        self.callbacks[event] = _callback

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url
        callback = self.callbacks.get("request")
        if callback is not None:
            callback(_Request(b"request-body"))

    async def wait_for_url(self, url: str, **_kwargs: object) -> None:
        assert self.url == url

    def get_by_role(self, role: str, *, name: str, exact: bool) -> _Counted:
        assert exact is True
        key = (role, name)
        if key == ("button", "Todos los servicios"):
            return _Action(self, "services")
        if key == ("link", "Clave Poder Judicial"):
            assert "services" in self.actions
            return _Action(self, "clave")
        if key == ("link", "Mis Causas"):
            return _Counted()
        raise AssertionError(key)

    def locator(self, selector: str) -> _Counted:
        if selector == "#segunda-clave-access":
            assert "clave" in self.actions
            return self.modal
        if selector == 'a[href="#infousuario"]':
            return _Counted()
        raise AssertionError(selector)

    async def close(self) -> None:
        self.actions.append("page-close")


class _Context:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.closed = False
        self.cdp = _Cdp()

    async def new_page(self) -> _Page:
        return self.page

    async def new_cdp_session(self, _page: _Page) -> "_Cdp":
        return self.cdp

    async def cookies(self) -> list[dict[str, object]]:
        return [{"name": "AUTH", "value": "authenticated-cookie", "domain": "oficinajudicialvirtual.pjud.cl", "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": -1}]

    async def close(self) -> None:
        self.closed = True


class _Cdp:
    def __init__(self) -> None:
        self.detached = False
        self.callback = None

    def on(self, event: str, callback: object) -> None:
        assert event == "Network.dataReceived"
        self.callback = callback

    async def send(self, command: str) -> None:
        assert command == "Network.enable"
        if self.callback is not None:
            self.callback({"encodedDataLength": 23})

    async def detach(self) -> None:
        self.detached = True


class _Browser:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.closed = False

    async def new_context(self, **kwargs: object) -> _Context:
        assert kwargs == {"user_agent": "official-test-agent"}
        return self.context

    async def close(self) -> None:
        self.closed = True


class _Chromium:
    def __init__(self, browser: _Browser) -> None:
        self.browser = browser
        self.launch_kwargs: dict[str, object] | None = None

    async def launch(self, **kwargs: object) -> _Browser:
        self.launch_kwargs = kwargs
        return self.browser


class _PlaywrightManager:
    def __init__(self, chromium: _Chromium) -> None:
        self.chromium = chromium

    async def __aenter__(self) -> "_PlaywrightManager":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _install_fake_browser(monkeypatch: pytest.MonkeyPatch, page: _Page) -> tuple[_Context, _Browser]:
    import app.ojv.browser_login as browser_login

    context = _Context(page)
    browser = _Browser(context)
    monkeypatch.setattr(
        browser_login, "async_playwright", lambda: _PlaywrightManager(_Chromium(browser)),
    )
    return context, browser


async def test_official_adapter_uses_observed_ui_and_returns_owned_typed_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()
    context, browser = _install_fake_browser(monkeypatch, page)

    result = await login_official_ojv(
        SecretStr("11.111.111-1"),
        SecretStr("synthetic-password-never-log"),
        proxy_url="http://proxy-user:proxy-password@proxy.example:8080",
        user_agent="official-test-agent",
    )

    assert page.actions[:3] == ["services", "clave", "submit"]
    assert page.form.rut.filled == ["11111111"]
    assert page.form.password.filled == ["synthetic-password-never-log"]
    assert result.cookies[0].name == "AUTH"
    assert result.user_agent == "official-test-agent"
    # The launch arguments were cleared after use, which also drops proxy credentials.
    # Context/user-agent and UI calls above prove the requested browser configuration.
    assert page.events == ["request", "response"]
    assert browser.closed is True
    assert context.closed is True
    assert context.cdp.detached is True


async def test_official_adapter_fails_closed_before_filling_when_entry_origin_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WrongOriginPage(_Page):
        async def goto(self, _url: str, **_kwargs: object) -> None:
            self.url = "https://ojv.pjud.cl/kpitec-ojv-web/views/login_pjud.html"

    page = WrongOriginPage()
    _install_fake_browser(monkeypatch, page)
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert page.form.rut.filled == []
    assert page.form.password.filled == []


async def test_official_adapter_requires_authenticated_account_and_mis_causas_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingMarkerPage(_Page):
        def locator(self, selector: str) -> _Counted:
            if selector == 'a[href="#infousuario"]':
                return _Counted(0)
            return super().locator(selector)

    page = MissingMarkerPage()
    _install_fake_browser(monkeypatch, page)
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")


@pytest.mark.parametrize("missing", ["account", "my_causes"])
async def test_landing_failure_identifies_marker_with_redacted_counts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, missing: str,
) -> None:
    class LandingPage(_Page):
        def locator(self, selector: str) -> _Counted:
            if selector == 'a[href="#infousuario"]':
                return _Counted(0 if missing == "account" else 1)
            return super().locator(selector)

        def get_by_role(self, role: str, *, name: str, exact: bool) -> _Counted:
            if name == "Mis Causas":
                return _Counted(0 if missing == "my_causes" else 1)
            return super().get_by_role(role, name=name, exact=exact)

        async def evaluate(self, _script: str) -> str:
            return "complete"

    _install_fake_browser(monkeypatch, LandingPage())
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("never-log-password"), proxy_url=None, user_agent="official-test-agent")
    message = next(record.getMessage() for record in caplog.records if record.name == "app.ojv.browser_login")
    assert f"stage=landing_{missing}" in message
    assert "landing_ready=complete" in message
    assert f"account_shape={'0,0' if missing == 'account' else '1,1'}" in message
    assert f"my_causes_shape={'0,0' if missing == 'my_causes' else '1,1'}" in message
    assert "never-log-password" not in message


async def test_slow_landing_probe_stays_within_login_deadline() -> None:
    from app.ojv.browser_login import _LandingProbe, _sample_landing

    class SlowCount(_Counted):
        async def count(self):
            await asyncio.sleep(5)

    class SlowPage(_Page):
        def locator(self, _selector):
            return SlowCount()

    page = SlowPage()
    page.url = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
    probe = _LandingProbe()
    start = time.monotonic()
    await _sample_landing(page, probe, start + 0.02)
    assert time.monotonic() - start < 0.2
    assert probe.account == (-1, -1)


async def test_landing_probe_failure_cannot_reject_an_authenticated_session(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    class BadDiagnosticPage(_Page):
        async def evaluate(self, _script):
            raise RuntimeError("private-provider-message-must-not-escape")

    context, browser = _install_fake_browser(monkeypatch, BadDiagnosticPage())
    result = await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert result.cookies[0].name == "AUTH"
    assert context.closed and browser.closed
    assert not [record for record in caplog.records if record.name == "app.ojv.browser_login"]


async def test_landing_watcher_cancel_is_drained_before_browser_close(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ojv.browser_login as adapter
    stopped = False

    async def watch(*_args):
        nonlocal stopped
        try:
            await asyncio.sleep(5)
        finally:
            stopped = True

    class LandingYieldPage(_Page):
        def get_by_role(self, role, *, name, exact):
            if name == "Mis Causas":
                class YieldCount(_Counted):
                    async def wait_for(self, **_kwargs):
                        await asyncio.sleep(0.01)
                return YieldCount()
            return super().get_by_role(role, name=name, exact=exact)

    monkeypatch.setattr(adapter, "_watch_landing", watch)
    _install_fake_browser(monkeypatch, LandingYieldPage())
    await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert stopped


async def test_landing_redirect_during_marker_wait_is_rejected_and_classified(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    class RedirectPage(_Page):
        def get_by_role(self, role, *, name, exact):
            if name == "Mis Causas":
                page = self
                class RedirectMarker(_Counted):
                    async def wait_for(self, **_kwargs):
                        page.url = "https://unexpected.invalid/?token=never-log-token"
                return RedirectMarker()
            return super().get_by_role(role, name=name, exact=exact)

    context, browser = _install_fake_browser(monkeypatch, RedirectPage())
    cookie_reads = []
    original_cookies = context.cookies
    async def observe_cookie_read():
        cookie_reads.append(True)
        return await original_cookies()
    context.cookies = observe_cookie_read
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    message = next(record.getMessage() for record in caplog.records if record.name == "app.ojv.browser_login")
    assert "stage=landing_final_url" in message
    assert "landing_location=untrusted" in message
    assert "unexpected.invalid" not in message
    assert "never-log-token" not in message
    assert cookie_reads == []
    assert context.closed and browser.closed


async def test_explicit_rejection_is_credential_invalid_without_provider_exception_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RejectedPage(_Page):
        def __init__(self) -> None:
            super().__init__()
            submit = self.form.get_by_role("button", name="Ingresar", exact=True)

            async def reject(**_kwargs: object) -> None:
                self.actions.append("submit")

            submit.click = reject  # type: ignore[method-assign]
            self.form.get_by_role = lambda *_args, **_kwargs: submit  # type: ignore[method-assign]

        async def wait_for_url(self, _url: str, **_kwargs: object) -> None:
            raise PlaywrightTimeoutError("provider secret must not escape")

        def locator(self, selector: str) -> _Counted:
            if ":visible" in selector:
                return _VisibleAlerts(["RUT o contraseña incorrectos"])
            return super().locator(selector)

    page = RejectedPage()
    _install_fake_browser(monkeypatch, page)
    with pytest.raises(InvalidCredentialsError) as exc_info:
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert "provider secret" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


async def test_browser_timeout_is_closed_and_resources_are_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutPage(_Page):
        async def goto(self, _url: str, **_kwargs: object) -> None:
            raise TimeoutError()

    page = TimeoutPage()
    context, browser = _install_fake_browser(monkeypatch, page)
    with pytest.raises(OjvTimeoutError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert context.closed is True
    assert browser.closed is True


class _VisibleAlerts(_Counted):
    def __init__(self, messages: list[str]) -> None:
        super().__init__(len(messages))
        self.messages = messages

    async def all_inner_texts(self) -> list[str]:
        return self.messages


class _Request:
    def __init__(self, body: bytes) -> None:
        self.post_data_buffer = body


async def test_origin_change_during_rut_fill_prevents_password_and_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OriginChangingPage(_Page):
        def __init__(self) -> None:
            super().__init__()
            original_fill = self.form.rut.fill

            async def change_origin(value: str, **kwargs: object) -> None:
                await original_fill(value, **kwargs)
                self.url = "https://attacker.invalid/login"

            self.form.rut.fill = change_origin  # type: ignore[method-assign]

    page = OriginChangingPage()
    _install_fake_browser(monkeypatch, page)
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert page.form.password.filled == []
    assert "submit" not in page.actions


async def test_adapter_403_preserves_familia_blocked_error_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 403

    class BlockedPage(_Page):
        async def goto(self, url: str, **kwargs: object) -> Response:
            await super().goto(url, **kwargs)
            return Response()

    _install_fake_browser(monkeypatch, BlockedPage())
    with pytest.raises(FamiliaBlockedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")


async def test_adapter_request_callback_stays_in_active_usage_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()
    _install_fake_browser(monkeypatch, page)
    with capture_proxy_usage() as usage:
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert usage.request_count == 1
    assert usage.bytes_up == len(b"request-body")
    assert usage.bytes_down == 23


async def test_malformed_browser_cookie_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BadCookiesContext(_Context):
        async def cookies(self) -> list[dict[str, object]]:
            return [{"name": "AUTH", "value": "secret", "domain": "", "path": "/"}]

    import app.ojv.browser_login as browser_login
    page = _Page()
    context = BadCookiesContext(page)
    browser = _Browser(context)
    monkeypatch.setattr(browser_login, "async_playwright", lambda: _PlaywrightManager(_Chromium(browser)))
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")


async def test_ambiguous_browser_cookie_scope_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AmbiguousContext(_Context):
        async def cookies(self) -> list[dict[str, object]]:
            return [
                {"name": "AUTH", "value": "one", "domain": "oficinajudicialvirtual.pjud.cl", "path": "/"},
                {"name": "AUTH", "value": "two", "domain": "oficinajudicialvirtual.pjud.cl", "path": "/"},
            ]

    import app.ojv.browser_login as browser_login
    context = AmbiguousContext(_Page())
    monkeypatch.setattr(browser_login, "async_playwright", lambda: _PlaywrightManager(_Chromium(_Browser(context))))
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")


async def test_runtime_exit_failure_discards_cookie_result_from_adapter_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitFailure(_PlaywrightManager):
        async def __aexit__(self, *_args: object) -> None:
            raise RuntimeError("driver exit failed")

    import app.ojv.browser_login as browser_login
    page = _Page()
    manager = ExitFailure(_Chromium(_Browser(_Context(page))))
    monkeypatch.setattr(browser_login, "async_playwright", lambda: manager)
    with pytest.raises(OjvUpstreamChangedError) as exc_info:
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("password-secret"), proxy_url=None, user_agent="official-test-agent")
    frames: list[str] = []
    traceback = exc_info.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith("browser_login.py"):
            frames.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    rendered = " ".join(frames)
    assert "authenticated-cookie" not in rendered
    assert "password-secret" not in rendered


async def test_cancellation_during_one_cleanup_step_still_closes_remaining_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancelClosePage(_Page):
        async def close(self) -> None:
            raise asyncio.CancelledError()

    page = CancelClosePage()
    context, browser = _install_fake_browser(monkeypatch, page)
    with pytest.raises(asyncio.CancelledError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert context.closed is True
    assert browser.closed is True


async def test_hidden_authenticated_marker_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hidden(_Counted):
        async def wait_for(self, **_kwargs: object) -> None:
            raise PlaywrightTimeoutError("hidden")

    class HiddenMarkerPage(_Page):
        def locator(self, selector: str) -> _Counted:
            if selector == 'a[href="#infousuario"]':
                return Hidden()
            return super().locator(selector)

    _install_fake_browser(monkeypatch, HiddenMarkerPage())
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")


async def test_visible_account_marker_ignores_one_hidden_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MixedAccount(_Counted):
        def __init__(self) -> None:
            super().__init__(2)

        def filter(self, **_kwargs: object) -> _Counted:
            return _Counted(1)

    class MixedPage(_Page):
        def locator(self, selector: str) -> _Counted:
            if selector == 'a[href="#infousuario"]':
                return MixedAccount()
            return super().locator(selector)

    _install_fake_browser(monkeypatch, MixedPage())
    result = await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert result.cookies[0].name == "AUTH"


async def test_two_visible_account_markers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DuplicateAccount(_Counted):
        def __init__(self) -> None:
            super().__init__(2)

        def filter(self, **_kwargs: object) -> _Counted:
            return _Counted(2)

    class DuplicatePage(_Page):
        def locator(self, selector: str) -> _Counted:
            if selector == 'a[href="#infousuario"]':
                return DuplicateAccount()
            return super().locator(selector)

    _install_fake_browser(monkeypatch, DuplicatePage())
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")


async def test_hung_driver_startup_times_out_and_attempts_manager_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HungManager(_PlaywrightManager):
        def __init__(self) -> None:
            self.chromium = None
            self.exited = False

        async def __aenter__(self):
            await asyncio.sleep(1)

        async def __aexit__(self, *_args: object) -> None:
            self.exited = True

    import app.ojv.browser_login as browser_login
    manager = HungManager()
    monkeypatch.setattr(browser_login, "_LOGIN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(browser_login, "async_playwright", lambda: manager)
    with pytest.raises(OjvTimeoutError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert manager.exited is True


async def test_hung_cookie_export_cannot_succeed_after_login_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowCookiesContext(_Context):
        async def cookies(self) -> list[dict[str, object]]:
            await asyncio.sleep(1)
            return await super().cookies()

    import app.ojv.browser_login as browser_login
    context = SlowCookiesContext(_Page())
    monkeypatch.setattr(browser_login, "_LOGIN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(browser_login, "async_playwright", lambda: _PlaywrightManager(_Chromium(_Browser(context))))
    with pytest.raises(OjvTimeoutError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert context.closed is True


async def test_post_submit_classifier_never_awaits_alert_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowAlertPage(_Page):
        def __init__(self) -> None:
            super().__init__()
            submit = self.form.get_by_role("button", name="Ingresar", exact=True)

            async def stay_put(**_kwargs: object) -> None:
                self.actions.append("submit")

            submit.click = stay_put  # type: ignore[method-assign]
            self.form.get_by_role = lambda *_args, **_kwargs: submit  # type: ignore[method-assign]

        async def wait_for_url(self, _url: str, **_kwargs: object) -> None:
            raise PlaywrightTimeoutError("not landed")

        def locator(self, selector: str) -> _Counted:
            if ":visible" in selector:
                return _SlowAlerts(["RUT o contraseña incorrectos"])
            return super().locator(selector)

    class _SlowAlerts(_VisibleAlerts):
        async def all_inner_texts(self) -> list[str]:
            await asyncio.sleep(0.08)
            return await super().all_inner_texts()

    import app.ojv.browser_login as browser_login
    monkeypatch.setattr(browser_login, "_LOGIN_TIMEOUT_S", 0.01)
    _install_fake_browser(monkeypatch, SlowAlertPage())
    with pytest.raises(OjvTimeoutError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")


async def test_expired_landing_resolves_timeout_without_any_ui_lookup() -> None:
    class LandingAtExpiry:
        url = "https://oficinajudicialvirtual.pjud.cl/indexN.php"

        def locator(self, _selector: str) -> object:
            raise AssertionError("post-expiry UI lookup")

    result = await _resolve_post_submit(LandingAtExpiry(), time.monotonic() - 0.001)
    assert isinstance(result, OjvTimeoutError)


@pytest.mark.parametrize("boundary", [
    "browser_launch", "context_create", "page_create", "entry_goto",
    "rut_fill", "password_fill", "cookie_snapshot", "runtime_exit",
])
async def test_failure_diagnostic_identifies_boundary_without_provider_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, boundary: str,
) -> None:
    import app.ojv.browser_login as adapter

    page = _Page()
    context = _Context(page)
    browser = _Browser(context)
    chromium = _Chromium(browser)
    manager = _PlaywrightManager(chromium)
    targets = {
        "browser_launch": (chromium, "launch"),
        "context_create": (browser, "new_context"),
        "page_create": (context, "new_page"),
        "entry_goto": (page, "goto"),
        "rut_fill": (page.form.rut, "fill"),
        "password_fill": (page.form.password, "fill"),
        "cookie_snapshot": (context, "cookies"),
        "runtime_exit": (manager, "__aexit__"),
    }

    async def fail(*_args, **_kwargs):
        raise RuntimeError("password=synthetic-sensitive https://private.invalid/?token=secret")

    target, method = targets[boundary]
    monkeypatch.setattr(target, method, fail)
    monkeypatch.setattr(adapter, "async_playwright", lambda: manager)
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("synthetic-sensitive"), proxy_url=None, user_agent="official-test-agent")
    records = [record for record in caplog.records if record.name == "app.ojv.browser_login"]
    assert len(records) == 1
    assert records[0].getMessage().startswith(f"pjud_private_login_failed stage={boundary} outcome=upstream_changed")
    assert records[0].exc_info is None
    assert records[0].stack_info is None
    assert "synthetic-sensitive" not in repr(records[0].__dict__)
    assert "private.invalid" not in repr(records[0].__dict__)


async def test_cdp_failure_diagnostic_preserves_original_stage_through_exit_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import app.ojv.browser_login as adapter

    context = _Context(_Page())
    manager = _PlaywrightManager(_Chromium(_Browser(context)))

    async def fail(*_args, **_kwargs):
        raise RuntimeError("sensitive-provider-details")

    monkeypatch.setattr(context, "new_cdp_session", fail)
    monkeypatch.setattr(manager, "__aexit__", fail)
    monkeypatch.setattr(adapter, "async_playwright", lambda: manager)
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    records = [record for record in caplog.records if record.name == "app.ojv.browser_login"]
    assert len(records) == 1
    assert records[0].getMessage().startswith("pjud_private_login_failed stage=cdp_enable outcome=upstream_changed")


async def test_success_and_cancellation_do_not_emit_login_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    page = _Page()
    _install_fake_browser(monkeypatch, page)
    await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")

    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(page, "goto", cancel)
    with pytest.raises(asyncio.CancelledError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert not [record for record in caplog.records if record.name == "app.ojv.browser_login"]


@pytest.mark.parametrize("missing", ["modal", "form", "rut_input", "password_input", "submit_button", "rut_placeholder"])
async def test_form_diagnostic_identifies_missing_requirement_before_submit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, missing: str,
) -> None:
    page = _Page()
    submit = page.form.get_by_role("button", name="Ingresar", exact=True)
    monkeypatch.setattr(page.form, "get_by_role", lambda *_args, **_kwargs: submit)
    targets = {
        "modal": page.modal, "form": page.form, "rut_input": page.form.rut,
        "password_input": page.form.password, "submit_button": submit,
    }
    if missing == "rut_placeholder":
        page.form.rut._placeholder = "private unexpected placeholder must not be logged"
    else:
        targets[missing]._count = 0
    context, browser = _install_fake_browser(monkeypatch, page)
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert page.form.rut.filled == (["11111111"] if missing == "submit_button" else [])
    assert page.form.password.filled == (["secret"] if missing == "submit_button" else [])
    assert "submit" not in page.actions
    assert context.closed and browser.closed
    records = [record for record in caplog.records if record.name == "app.ojv.browser_login"]
    assert len(records) == 1
    assert records[0].getMessage().startswith(f"pjud_private_login_failed stage={missing} outcome=upstream_changed")


@pytest.mark.parametrize("error,kind,network,outcome", [
    (PlaywrightError("net::ERR_TUNNEL_CONNECTION_FAILED secret-token"), "browser_error", "tunnel_connection_failed", "upstream_changed"),
    (PlaywrightError("net::ERR_PROXY_CONNECTION_FAILED secret-token"), "browser_error", "proxy_connection_failed", "upstream_changed"),
    (PlaywrightError("net::ERR_CONNECTION_RESET secret-token"), "browser_error", "connection_reset", "upstream_changed"),
    (PlaywrightError("net::ERR_NAME_NOT_RESOLVED secret-token"), "browser_error", "name_not_resolved", "upstream_changed"),
    (PlaywrightError("net::ERR_UNKNOWN secret-token"), "browser_error", "other", "upstream_changed"),
    (PlaywrightTimeoutError("secret-token"), "timeout", "timeout", "timeout"),
    (TypeError("secret-token"), "type_error", "none", "upstream_changed"),
    (RuntimeError("net::ERR_CONNECTION_RESET secret-token"), "internal", "none", "upstream_changed"),
])
async def test_entry_failure_logs_only_closed_exception_and_network_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    error: Exception, kind: str, network: str, outcome: str,
) -> None:
    page = _Page()
    async def fail(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(page, "goto", fail)
    _install_fake_browser(monkeypatch, page)
    with pytest.raises((OjvTimeoutError, OjvUpstreamChangedError)):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret-token"), proxy_url=None, user_agent="official-test-agent")
    records = [record for record in caplog.records if record.name == "app.ojv.browser_login"]
    assert len(records) == 1
    assert records[0].getMessage() == (
        f"pjud_private_login_failed stage=entry_goto outcome={outcome} "
        f"kind={kind} network={network} entry_http=0 entry_origin=unknown "
        "landing_http=0 landing_ready=unavailable landing_location=unknown account_shape=-1,-1 my_causes_shape=-1,-1"
    )
    assert "secret-token" not in repr(records[0].__dict__)
    assert records[0].exc_info is None


@pytest.mark.parametrize("status,origin,expected_origin,error_type", [
    (404, "https://oficinajudicialvirtual.pjud.cl/home/index.php", "unknown", OjvUpstreamChangedError),
    (403, "https://oficinajudicialvirtual.pjud.cl/home/index.php", "unknown", FamiliaBlockedError),
    (200, "https://unexpected.invalid/?secret-token", "untrusted", OjvUpstreamChangedError),
])
async def test_entry_http_and_origin_failures_are_distinguishable_without_urls(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    status: int, origin: str, expected_origin: str, error_type: type[Exception],
) -> None:
    from types import SimpleNamespace
    page = _Page()
    async def response(*_args, **_kwargs):
        page.url = origin
        return SimpleNamespace(status=status)
    monkeypatch.setattr(page, "goto", response)
    _install_fake_browser(monkeypatch, page)
    with pytest.raises(error_type):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret-token"), proxy_url=None, user_agent="official-test-agent")
    records = [record for record in caplog.records if record.name == "app.ojv.browser_login"]
    assert len(records) == 1
    assert f"kind=contract network=none entry_http={status} entry_origin={expected_origin}" in records[0].getMessage()
    assert "secret-token" not in repr(records[0].__dict__)
    assert "unexpected.invalid" not in repr(records[0].__dict__)


async def test_submit_can_appear_after_verified_credentials_are_filled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()

    class ProgressiveSubmit(_Action):
        async def count(self) -> int:
            return 1 if page.form.rut.filled and page.form.password.filled else 0

    submit = ProgressiveSubmit(page, "submit")
    monkeypatch.setattr(page.form, "get_by_role", lambda *_args, **_kwargs: submit)
    _install_fake_browser(monkeypatch, page)
    result = await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert result.cookies[0].name == "AUTH"
    assert page.actions.count("submit") == 1


async def test_origin_change_while_waiting_for_submit_prevents_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()

    class RedirectingSubmit(_Action):
        async def wait_for(self, **_kwargs):
            page.url = "https://unexpected.invalid/"

    submit = RedirectingSubmit(page, "submit")
    monkeypatch.setattr(page.form, "get_by_role", lambda *_args, **_kwargs: submit)
    _install_fake_browser(monkeypatch, page)
    with pytest.raises(OjvUpstreamChangedError):
        await login_official_ojv(SecretStr("11.111.111-1"), SecretStr("secret"), proxy_url=None, user_agent="official-test-agent")
    assert page.form.password.filled == ["secret"]
    assert "submit" not in page.actions
