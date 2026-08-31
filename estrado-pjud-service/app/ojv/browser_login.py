"""Bounded, non-persistent browser login for OJV Mis Causas.

This adapter deliberately drives only the observed official UI.  It does not
reuse the caller's HTTP cookies, browser profile, or a cached context.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, async_playwright
from pydantic import SecretStr

from app.bandwidth import record_proxy_request, record_proxy_response_increment
from app.cookie_scope import CookieRecord, playwright_cookie_records
from app.minter import _ANTIBOT_ARGS
from app.ojv.errors import (
    FamiliaBlockedError,
    InvalidCredentialsError,
    OjvSessionError,
    OjvTimeoutError,
    OjvUpstreamChangedError,
    OjvWafError,
)
from app.proxy import split_proxy_for_playwright
from app.proxy_billing import ProxyBillingExhaustedError
from app.ojv.submit_diagnostics import _SubmitProbe


_OFFICIAL_ENTRY = "https://oficinajudicialvirtual.pjud.cl/home/index.php"
_OFFICIAL_LANDING = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
_OFFICIAL_HOST = "oficinajudicialvirtual.pjud.cl"
_LOGIN_TIMEOUT_S = 45.0
_CLEANUP_TIMEOUT_S = 1.0
_RUT_PLACEHOLDER = "Ingrese su Rut sin dígito verificador, Ej: 12345678"
logger = logging.getLogger(__name__)


def _exception_diagnostic(error: BaseException) -> tuple[str, str]:
    """Map browser failures to finite labels; never return upstream text."""
    if isinstance(error, (asyncio.TimeoutError, PlaywrightTimeoutError)):
        return "timeout", "timeout"
    if isinstance(error, PlaywrightError):
        message = ""
        match = None
        try:
            message = error.message
            match = re.search(r"\bnet::(ERR_[A-Z_]+)\b", message) if isinstance(message, str) else None
            return "browser_error", {
                "ERR_TUNNEL_CONNECTION_FAILED": "tunnel_connection_failed",
                "ERR_PROXY_CONNECTION_FAILED": "proxy_connection_failed",
                "ERR_CONNECTION_RESET": "connection_reset",
                "ERR_NAME_NOT_RESOLVED": "name_not_resolved",
            }.get(match.group(1) if match else "", "other")
        except Exception:
            return "browser_error", "other"
        finally:
            message = ""
            match = None
    if isinstance(error, TypeError):
        return "type_error", "none"
    return "internal", "none"


def _rut_body(rut: str) -> str:
    normalized = rut.replace(".", "").strip().upper()
    if "-" in normalized:
        return normalized.rsplit("-", 1)[0][:8]
    clean = normalized.replace("-", "")
    return (clean[:-1] if len(clean) >= 9 else clean)[:8]


def _explicit_credential_rejection(html: str) -> bool:
    lower = html.lower()
    return any(marker in lower for marker in (
        "gob-response-error", "clave incorrecta", "rut o clave",
        "rut o contraseña", "rut o constraseña", "credenciales inválidas",
        "contraseña incorrecta", "rut incorrecto", "usuario no encontrado",
        "clave poder judicial incorrecta", "rut no registrado",
    ))


@dataclass(frozen=True, slots=True)
class BrowserLoginResult:
    """Authenticated cookies obtained in one owned browser context."""

    cookies: tuple[CookieRecord, ...]
    user_agent: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.user_agent, str)
            or not self.user_agent
            or not self.cookies
            or any(not isinstance(cookie, CookieRecord) for cookie in self.cookies)
        ):
            raise ValueError("invalid_browser_login_result")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(cookie_count={len(self.cookies)}, user_agent=<redacted>)"


def _is_trusted_official_url(url: str, *, landing: bool = False) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == _OFFICIAL_HOST
        and parsed.port in {None, 443}
        and (not landing or parsed.path == "/indexN.php")
    )


def _remaining_timeout_ms(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError()
    return max(1, int(remaining * 1000))


async def _within_deadline(awaitable: object, deadline: float):
    try:
        timeout = _remaining_timeout_ms(deadline) / 1000
    except asyncio.TimeoutError:
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise
    return await asyncio.wait_for(awaitable, timeout=timeout)


async def _close(resource: object | None) -> None:
    if resource is None:
        return
    close = getattr(resource, "close", None)
    if close is None:
        return
    try:
        await asyncio.wait_for(close(), timeout=_CLEANUP_TIMEOUT_S)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        # Cleanup must not extend an OJV attempt or expose provider errors.
        return


async def _create_cdp_meter(context: object, page: object) -> object | None:
    new_cdp_session = getattr(context, "new_cdp_session", None)
    if new_cdp_session is None:
        return None
    session = None
    try:
        session = await asyncio.wait_for(
            new_cdp_session(page), timeout=_CLEANUP_TIMEOUT_S,
        )

        def record_data(event: object) -> None:
            if isinstance(event, dict):
                record_proxy_response_increment(event.get("encodedDataLength"))

        session.on("Network.dataReceived", record_data)
        await asyncio.wait_for(session.send("Network.enable"), timeout=_CLEANUP_TIMEOUT_S)
        return session
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        await _close(session)
        return None


async def _detach_cdp_meter(session: object | None) -> None:
    if session is None:
        return
    detach = getattr(session, "detach", None)
    if detach is None:
        return
    try:
        await asyncio.wait_for(detach(), timeout=_CLEANUP_TIMEOUT_S)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return


async def _cleanup_all(*resources: tuple[object | None, str]) -> bool:
    """Best-effort bounded cleanup; return whether cancellation was observed."""
    cancelled = False
    for resource, kind in resources:
        try:
            if kind == "cdp":
                await _detach_cdp_meter(resource)
            else:
                await _close(resource)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


async def _wait_visible(locator: object, deadline: float) -> bool:
    try:
        visible = locator.filter(visible=True)
        await _within_deadline(visible.wait_for(state="visible", timeout=_remaining_timeout_ms(deadline)), deadline)
        return await _within_deadline(visible.count(), deadline) == 1
    except (asyncio.TimeoutError, PlaywrightTimeoutError):
        return False


@dataclass(slots=True)
class _LandingProbe:
    """Closed diagnostic values only; samples are advisory, not auth evidence."""

    main_http: int = 0
    ready: str = "unavailable"
    location: str = "unknown"
    account: tuple[int, int] = (-1, -1)
    my_causes: tuple[int, int] = (-1, -1)


def _record_landing_response(page: object, probe: _LandingProbe, response: object) -> None:
    try:
        request = response.request
        if (
            request.is_navigation_request() and request.frame == page.main_frame
            and _is_trusted_official_url(response.url, landing=True)
            and type(response.status) is int and 100 <= response.status <= 599
        ):
            probe.main_http = response.status
    except Exception:
        pass  # Never expose response/URL/exception objects in diagnostics.


async def _sample_landing(page: object, probe: _LandingProbe, deadline: float) -> None:
    """Best-effort sample under the existing login deadline, capped at 500ms."""
    sample_deadline = min(deadline, time.monotonic() + 0.5)
    try:
        if not _is_trusted_official_url(page.url, landing=True):
            return
        for field, locator in (
            ("account", page.locator('a[href="#infousuario"]')),
            ("my_causes", page.get_by_role("link", name="Mis Causas", exact=True)),
        ):
            counts = []
            for target in (locator, locator.filter(visible=True)):
                count = await _within_deadline(target.count(), sample_deadline)
                counts.append(min(count, 9) if type(count) is int and count >= 0 else -1)
            setattr(probe, field, tuple(counts))
        ready = await _within_deadline(page.evaluate("() => document.readyState"), sample_deadline)
        probe.ready = ready if ready in {"loading", "interactive", "complete"} else "other"
    except Exception:
        # Retain the last known partial sample. Sampling cannot accept/reject a
        # session, but its bounded initial read consumes existing deadline time.
        pass


async def _watch_landing(page: object, probe: _LandingProbe, deadline: float) -> None:
    try:
        while time.monotonic() < deadline:
            await _within_deadline(asyncio.sleep(0.5), deadline)
            await _sample_landing(page, probe, deadline)
    except (asyncio.TimeoutError, PlaywrightTimeoutError):
        return


def _record_request(request: object) -> None:
    """Count request bytes only; never retain a request body."""
    body = b""
    try:
        body = getattr(request, "post_data_buffer", None) or b""
        record_proxy_request(len(body))
    except BaseException:
        record_proxy_request(0)
    finally:
        body = b""


def _submit_location(page: object) -> str:
    try:
        url = page.url
        if not _is_trusted_official_url(url):
            return "untrusted"
        path = urlparse(url).path
        return {"/home/index.php": "official_entry", "/indexN.php": "official_landing"}.get(path, "official_other")
    except Exception:
        return "unavailable"


async def _page_has_visible_credential_rejection(page: object, probe: _SubmitProbe | None = None) -> bool:
    """Classify only an explicit, currently visible post-submit alert."""
    messages: list[str] = []
    try:
        alerts = page.locator(
            '[role="alert"]:visible, .alert-danger:visible, .error:visible, '
            '.invalid-feedback:visible'
        )
        messages = await alerts.all_inner_texts()
        return any(_explicit_credential_rejection(message) for message in messages)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        if probe is not None:
            probe.inspection_failed = True
        return False
    finally:
        messages.clear()


async def _page_has_visible_challenge(page: object, probe: _SubmitProbe | None = None) -> bool:
    try:
        # The official entry page displays a reCAPTCHA badge even before login.
        # Its logo iframe is not an interactive challenge. Keep stopping for
        # visible challenge frames outside that badge and for challenge widgets.
        challenge = page.locator(
            'iframe[title*="captcha" i]:visible:not(.grecaptcha-badge iframe), '
            '.g-recaptcha:visible, [data-sitekey]:visible'
        )
        return await challenge.count() > 0
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        if probe is not None:
            probe.inspection_failed = True
        return False


async def _resolve_post_submit(page: object, deadline: float, probe: _SubmitProbe | None = None) -> OjvSessionError | None:
    """Observe post-submit state only while login time remains."""
    while True:
        try:
            _remaining_timeout_ms(deadline)
        except asyncio.TimeoutError:
            return OjvTimeoutError()
        if _is_trusted_official_url(page.url, landing=True):
            return None
        try:
            if _is_trusted_official_url(page.url):
                if probe is not None:
                    probe.increment("inspection_attempts")
                    probe.inspection_failed = False
                    probe.inspection = "inspection_unavailable"
                if await _within_deadline(_page_has_visible_credential_rejection(page, probe), deadline):
                    if probe is not None:
                        probe.inspection = "rejection"
                    return InvalidCredentialsError()
                if await _within_deadline(_page_has_visible_challenge(page, probe), deadline):
                    if probe is not None:
                        probe.inspection = "challenge"
                    return FamiliaBlockedError()
                if probe is not None and not probe.inspection_failed:
                    probe.inspection = "checked_none"
            await _within_deadline(asyncio.sleep(0.05), deadline)
        except (asyncio.TimeoutError, PlaywrightTimeoutError):
            return OjvTimeoutError()


async def login_official_ojv(
    rut: SecretStr,
    password: SecretStr,
    *,
    proxy_url: str | None,
    user_agent: str,
) -> BrowserLoginResult:
    """Authenticate through the fixed OJV UI and return fresh typed cookies.

    All provider/browser failures are converted to the existing closed error
    taxonomy.  Cancellation is intentionally propagated to preserve worker
    ownership and deadline semantics.
    """
    if not isinstance(rut, SecretStr) or not isinstance(password, SecretStr):
        raise TypeError("credentials must be SecretStr")
    if not isinstance(user_agent, str) or not user_agent:
        raise ValueError("user_agent must be a non-empty string")

    deadline = time.monotonic() + _LOGIN_TIMEOUT_S
    manager = async_playwright()
    browser = context = page = cdp_session = None
    result: BrowserLoginResult | None = None
    failure: OjvSessionError | None = None
    submitted = False
    cancelled = False
    manager_entered = False
    rut_value = password_value = rut_digits = ""
    launch_kwargs: dict[str, object] = {"headless": False, "args": _ANTIBOT_ARGS}
    # Only literals assigned locally: never attach URLs, exception objects,
    # browser state, credentials or cookie contents to diagnostic records.
    stage = "proxy_config"
    error_kind, network_code = "contract", "none"
    entry_http = 0
    entry_origin = "unknown"
    landing_probe = _LandingProbe()
    submit_probe = _SubmitProbe()
    landing_observer = None
    try:
        if proxy_url:
            launch_kwargs["proxy"] = split_proxy_for_playwright(proxy_url)
        proxy_url = None
        stage = "playwright_enter"
        playwright = await _within_deadline(manager.__aenter__(), deadline)
        manager_entered = True
        try:
            stage = "browser_launch"
            browser = await _within_deadline(playwright.chromium.launch(**launch_kwargs), deadline)
            launch_kwargs.clear()
            stage = "context_create"
            context = await _within_deadline(browser.new_context(user_agent=user_agent), deadline)
            stage = "page_create"
            page = await _within_deadline(context.new_page(), deadline)
            page.on("request", _record_request)
            page.on("response", lambda response: _record_landing_response(page, landing_probe, response))
            stage = "cdp_enable"
            cdp_session = await _within_deadline(_create_cdp_meter(context, page), deadline)
            if cdp_session is None:
                failure = OjvUpstreamChangedError()
            if failure is None:
                stage = "entry_goto"
                response = await _within_deadline(page.goto(
                    _OFFICIAL_ENTRY, wait_until="domcontentloaded", timeout=_remaining_timeout_ms(deadline),
                ), deadline)
                status = response.status if response is not None else 200
                if type(status) is int and 100 <= status <= 599:
                    entry_http = status
                if status in {403, 429}:
                    failure = FamiliaBlockedError()
                elif status == 408 or status >= 500:
                    failure = OjvTimeoutError()
                elif status >= 400:
                    failure = OjvUpstreamChangedError()
                else:
                    entry_origin = "trusted" if _is_trusted_official_url(page.url) else "untrusted"
                    if entry_origin == "untrusted":
                        failure = OjvUpstreamChangedError()
            if failure is None:
                stage = "services_visible"
                services = page.get_by_role("button", name="Todos los servicios", exact=True)
                if not await _wait_visible(services, deadline):
                    failure = OjvUpstreamChangedError()
            if failure is None:
                stage = "services_click"
                await _within_deadline(services.click(timeout=_remaining_timeout_ms(deadline)), deadline)
                stage = "clave_visible"
                clave = page.get_by_role("link", name="Clave Poder Judicial", exact=True)
                if not await _wait_visible(clave, deadline):
                    failure = OjvUpstreamChangedError()
            if failure is None:
                stage = "clave_click"
                await _within_deadline(clave.click(timeout=_remaining_timeout_ms(deadline)), deadline)
                stage = "form_visible"
                modal = page.locator("#segunda-clave-access")
                form = modal.locator("#fSGN")
                rut_input = form.locator("input[type=text]")
                password_input = form.locator("input[type=password]")
                # OJV leaves aria-hidden=true on its visibly open login modal.
                # Include accessibility-hidden matches, but both wait and click
                # must still target a unique physically visible button in this form.
                submit = form.get_by_role(
                    "button", name="Ingresar", exact=True, include_hidden=True,
                ).filter(visible=True)
                for stage, item in (
                    ("modal", modal), ("form", form), ("rut_input", rut_input),
                    ("password_input", password_input),
                ):
                    if not await _wait_visible(item, deadline):
                        failure = OjvUpstreamChangedError()
                        break
                if failure is None:
                    stage = "rut_placeholder"
                    if await _within_deadline(rut_input.get_attribute("placeholder"), deadline) != _RUT_PLACEHOLDER:
                        failure = OjvUpstreamChangedError()
            if failure is None:
                stage = "rut_fill"
                rut_value = rut.get_secret_value()
                password_value = password.get_secret_value()
                rut_digits = _rut_body(rut_value)
                if not rut_digits.isdigit() or not _is_trusted_official_url(page.url):
                    failure = OjvUpstreamChangedError()
                else:
                    await _within_deadline(rut_input.fill(rut_digits, timeout=_remaining_timeout_ms(deadline)), deadline)
                    if not _is_trusted_official_url(page.url):
                        failure = OjvUpstreamChangedError()
                    else:
                        stage = "password_fill"
                        await _within_deadline(password_input.fill(password_value, timeout=_remaining_timeout_ms(deadline)), deadline)
                        if not _is_trusted_official_url(page.url):
                            failure = OjvUpstreamChangedError()
                        else:
                            stage = "submit_button"
                            if not await _wait_visible(submit, deadline) or not _is_trusted_official_url(page.url):
                                failure = OjvUpstreamChangedError()
                            else:
                                stage = "submit"
                                submit_probe.start(page, deadline)
                                await _within_deadline(submit.click(timeout=_remaining_timeout_ms(deadline)), deadline)
                                submitted = True
            if failure is None and submitted:
                stage = "post_submit"
                failure = await _resolve_post_submit(page, deadline, submit_probe)
                submit_probe.location = _submit_location(page)
                submit_probe.stop(page, deadline)
            if failure is None and submitted:
                stage = "landing_url"
                if not _is_trusted_official_url(page.url, landing=True):
                    failure = OjvUpstreamChangedError()
                else:
                    account = page.locator('a[href="#infousuario"]')
                    my_causes = page.get_by_role("link", name="Mis Causas", exact=True)
                    await _sample_landing(page, landing_probe, deadline)
                    landing_observer = asyncio.create_task(_watch_landing(page, landing_probe, deadline))
                    stage = "landing_account"
                    if not await _wait_visible(account, deadline):
                        failure = OjvUpstreamChangedError()
                    else:
                        stage = "landing_my_causes"
                        if not await _wait_visible(my_causes, deadline):
                            failure = OjvUpstreamChangedError()
            if failure is None and submitted:
                stage = "landing_final_url"
                if not _is_trusted_official_url(page.url, landing=True):
                    failure = OjvUpstreamChangedError()
            if failure is None and submitted:
                stage = "cookie_snapshot"
                result = BrowserLoginResult(
                    playwright_cookie_records(await _within_deadline(context.cookies(), deadline)), user_agent=user_agent,
                )
        except asyncio.CancelledError:
            cancelled = True
        except ProxyBillingExhaustedError:
            raise
        except (asyncio.TimeoutError, PlaywrightTimeoutError) as error:
            error_kind, network_code = _exception_diagnostic(error)
            failure = OjvTimeoutError()
        except BaseException as error:
            error_kind, network_code = _exception_diagnostic(error)
            failure = OjvUpstreamChangedError()
        finally:
            if submit_probe.active:
                submit_probe.location = _submit_location(page)
                submit_probe.stop(page, deadline)
            if submitted and page is not None:
                try:
                    landing_probe.location = (
                        "trusted_landing" if _is_trusted_official_url(page.url, landing=True)
                        else "trusted_other" if _is_trusted_official_url(page.url)
                        else "untrusted"
                    )
                except Exception:
                    pass
            if landing_observer is not None:
                landing_observer.cancel()
                try:
                    await asyncio.wait_for(asyncio.gather(landing_observer, return_exceptions=True), timeout=_CLEANUP_TIMEOUT_S)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    pass
                landing_observer = None
            cancelled = await _cleanup_all((cdp_session, "cdp"), (page, "resource"), (context, "resource"), (browser, "resource")) or cancelled
            cdp_session = page = context = browser = None
            try:
                if failure is None:
                    stage = "runtime_exit"
                await asyncio.wait_for(manager.__aexit__(None, None, None), timeout=_CLEANUP_TIMEOUT_S)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException as error:
                if failure is None:
                    error_kind, network_code = _exception_diagnostic(error)
                    failure = OjvUpstreamChangedError()
            if failure is not None or cancelled:
                result = None
    except asyncio.CancelledError:
        cancelled = True
    except ProxyBillingExhaustedError:
        raise
    except (asyncio.TimeoutError, PlaywrightTimeoutError) as error:
        error_kind, network_code = _exception_diagnostic(error)
        failure = OjvTimeoutError()
    except BaseException as error:
        error_kind, network_code = _exception_diagnostic(error)
        failure = OjvUpstreamChangedError()
    finally:
        if not manager_entered:
            try:
                await asyncio.wait_for(manager.__aexit__(None, None, None), timeout=_CLEANUP_TIMEOUT_S)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException as error:
                if failure is None:
                    error_kind, network_code = _exception_diagnostic(error)
                    failure = OjvUpstreamChangedError()
        rut_value = password_value = rut_digits = ""
        proxy_url = None
        launch_kwargs.clear()
        result = None if failure is not None or cancelled else result
    if cancelled:
        raise asyncio.CancelledError()
    if failure is not None:
        logger.warning(
            "pjud_private_login_failed stage=%s outcome=%s kind=%s network=%s entry_http=%s entry_origin=%s "
            "landing_http=%s landing_ready=%s landing_location=%s account_shape=%s my_causes_shape=%s %s",
            stage, failure.code.value, error_kind, network_code, entry_http, entry_origin,
            landing_probe.main_http, landing_probe.ready, landing_probe.location,
            ",".join(map(str, landing_probe.account)), ",".join(map(str, landing_probe.my_causes)),
            submit_probe.summary(),
        )
        raise failure from None
    if result is None:
        raise OjvUpstreamChangedError()
    return result
