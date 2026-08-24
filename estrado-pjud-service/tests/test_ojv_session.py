from __future__ import annotations

import logging

import httpx
import pytest
from pydantic import SecretStr

from app.ojv.errors import (
    InvalidCredentialsError,
    OjvSessionErrorCode,
    OjvTimeoutError,
    OjvUpstreamChangedError,
    OjvWafError,
    SessionExpiredError,
)
from app.ojv.session import OjvSession
from app.proxy_billing import ProxyBillingExhaustedError, is_proxy_billing_error


RUT = "11.111.111-1"
PASSWORD = "synthetic-password-never-log"
COOKIE = "synthetic-cookie-never-log"


def _secrets_are_absent(value: object) -> None:
    rendered = repr(value)
    assert RUT not in rendered
    assert "11111111" not in rendered
    assert PASSWORD not in rendered
    assert COOKIE not in rendered


def _production_traceback_locals(error: BaseException) -> str:
    frames: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if "/app/ojv/" in traceback.tb_frame.f_code.co_filename:
            frames.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return " ".join(frames)


async def test_clave_pj_login_preserves_redirect_and_cookie_sequence() -> None:
    requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.method,
                request.url.path,
                request.headers.get("cookie", ""),
            )
        )
        if request.url.path.endswith("login_pjud.html"):
            return httpx.Response(
                200,
                text="<html>login</html>",
                headers={"Set-Cookie": f"F5={COOKIE}; Path=/"},
                request=request,
            )
        if request.url.path.endswith("login_pjud"):
            payload = dict(httpx.QueryParams(request.content.decode()))
            assert payload == {"rutPjud": "11111111", "passwordPjud": PASSWORD}
            return httpx.Response(
                302,
                headers={
                    "Location": "https://oficinajudicialvirtual.pjud.cl/indexN.php"
                },
                request=request,
            )
        return httpx.Response(200, text="<html>Bienvenido</html>", request=request)

    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    await session.login(SecretStr(RUT), SecretStr(PASSWORD))

    assert requests == [
        ("GET", "/kpitec-ojv-web/views/login_pjud.html", ""),
        ("POST", "/kpitec-ojv-web/login_pjud", f"F5={COOKIE}"),
        ("GET", "/indexN.php", ""),
    ]
    assert session.authenticated_form_identity() == ("11111111", "1")
    await session.close()


async def test_login_redirect_without_invalid_body_is_session_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("login_pjud.html"):
            return httpx.Response(200, text="login", request=request)
        if request.url.path.endswith("login_pjud"):
            return httpx.Response(
                302,
                headers={
                    "Location": "https://oficinajudicialvirtual.pjud.cl/login.php"
                },
                request=request,
            )
        return httpx.Response(200, text="login form", request=request)

    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    with pytest.raises(SessionExpiredError) as exc_info:
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))

    assert exc_info.value.code is OjvSessionErrorCode.EXPIRED
    await session.close()


@pytest.mark.parametrize(
    ("response_status", "response_body", "error_type", "code"),
    [
        (200, "<p>RUT o contraseña incorrectos</p>", InvalidCredentialsError, "credential_invalid"),
        (419, "<html>expired csrf no existe</html>", SessionExpiredError, "session_expired"),
        (403, "<html>private provider body</html>", OjvWafError, "waf"),
        (503, "<html>temporary no existe</html>", OjvTimeoutError, "timeout"),
        (422, "<html>private provider body</html>", OjvUpstreamChangedError, "upstream_changed"),
    ],
)
async def test_login_has_exact_safe_terminal_taxonomy(
    response_status: int,
    response_body: str,
    error_type: type[Exception],
    code: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text="login", request=request)
        return httpx.Response(
            response_status,
            text=response_body,
            headers={"Set-Cookie": f"OJVID={COOKIE}; Path=/"},
            request=request,
        )

    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    with pytest.raises(error_type) as exc_info:
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))

    assert exc_info.value.code.value == code
    _secrets_are_absent(exc_info.value)
    assert response_body not in str(exc_info.value)
    await session.close()


async def test_login_timeout_has_safe_taxonomy_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(
            f"timeout body={COOKIE} rut={RUT}", request=request
        )

    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    with pytest.raises(OjvTimeoutError) as exc_info:
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))

    assert calls == 1
    assert exc_info.value.code is OjvSessionErrorCode.TIMEOUT
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    _secrets_are_absent(exc_info.value)
    traceback_locals = _production_traceback_locals(exc_info.value)
    assert RUT not in traceback_locals
    assert "11111111" not in traceback_locals
    assert PASSWORD not in traceback_locals
    assert COOKIE not in traceback_locals
    await session.close()


@pytest.mark.parametrize("status_code", [408, 503])
async def test_login_page_transient_status_uses_timeout_before_posting_credentials(
    status_code: int,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text="private timeout body", request=request)

    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    with pytest.raises(OjvTimeoutError) as exc_info:
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))

    assert len(requests) == 1
    assert exc_info.value.code is OjvSessionErrorCode.TIMEOUT
    await session.close()


async def test_login_page_waf_traceback_never_materializes_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text=f"<html>{RUT} {PASSWORD} {COOKIE} bobcmn</html>",
            request=request,
        )

    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    with pytest.raises(OjvWafError) as exc_info:
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))

    traceback_locals = _production_traceback_locals(exc_info.value)
    assert RUT not in traceback_locals
    assert "11111111" not in traceback_locals
    assert PASSWORD not in traceback_locals
    assert COOKIE not in traceback_locals
    await session.close()


async def test_transport_failure_is_redacted_into_the_closed_timeout_taxonomy() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text="login", request=request)
        raise httpx.ConnectError(
            f"proxy={COOKIE} rut={RUT} password={PASSWORD}", request=request
        )

    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    with pytest.raises(OjvTimeoutError) as exc_info:
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))

    assert exc_info.value.code is OjvSessionErrorCode.TIMEOUT
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    _secrets_are_absent(exc_info.value)
    traceback_locals = _production_traceback_locals(exc_info.value)
    assert RUT not in traceback_locals
    assert "11111111" not in traceback_locals
    assert PASSWORD not in traceback_locals
    assert COOKIE not in traceback_locals
    await session.close()


async def test_proxy_402_preserves_safe_billing_control_signal_without_chain() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text="login", request=request)
        raise httpx.ProxyError(
            f"402 Payment Required {RUT} {PASSWORD} {COOKIE}", request=request
        )

    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    with pytest.raises(ProxyBillingExhaustedError) as exc_info:
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))

    assert is_proxy_billing_error(exc_info.value) is True
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    _secrets_are_absent(exc_info.value)
    traceback_locals = _production_traceback_locals(exc_info.value)
    assert RUT not in traceback_locals
    assert "11111111" not in traceback_locals
    assert PASSWORD not in traceback_locals
    assert COOKIE not in traceback_locals
    await session.close()


async def test_session_requires_secret_wrappers_at_the_auth_boundary() -> None:
    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(lambda _: None))
    with pytest.raises(TypeError, match="SecretStr"):
        await session.login(RUT, PASSWORD)  # type: ignore[arg-type]
    await session.close()


async def test_close_is_idempotent_and_clears_identity_cookies_and_transport() -> None:
    session = OjvSession(
        rate_limit_s=0,
        cookies={"OJVID": COOKIE},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="unused", request=request)
        ),
    )
    session._remember_authenticated_rut(SecretStr(RUT))

    await session.close()
    await session.close()

    assert session._client.is_closed is True
    assert list(session._client.cookies.jar) == []
    with pytest.raises(SessionExpiredError) as exc_info:
        session.authenticated_form_identity()
    assert exc_info.value.code is OjvSessionErrorCode.EXPIRED
    _secrets_are_absent(session)


async def test_session_and_errors_never_log_or_render_sensitive_upstream_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_body = f"<html>{RUT} {PASSWORD} {COOKIE}</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("login_pjud.html"):
            return httpx.Response(200, text="login", request=request)
        return httpx.Response(422, text=private_body, request=request)

    session = OjvSession(rate_limit_s=0, transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(OjvUpstreamChangedError) as exc_info:
            await session.login(SecretStr(RUT), SecretStr(PASSWORD))

    rendered = f"{session!r} {vars(session)!r} {exc_info.value!s} {exc_info.value!r} {caplog.text}"
    assert RUT not in rendered
    assert "11111111" not in rendered
    assert PASSWORD not in rendered
    assert COOKIE not in rendered
    assert private_body not in rendered
    traceback_locals = _production_traceback_locals(exc_info.value)
    assert RUT not in traceback_locals
    assert "11111111" not in traceback_locals
    assert PASSWORD not in traceback_locals
    assert COOKIE not in traceback_locals
    assert private_body not in traceback_locals
    await session.close()
