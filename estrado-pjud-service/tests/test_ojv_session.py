from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.cookie_scope import CookieRecord
from app.ojv.browser_login import BrowserLoginResult
from app.ojv.errors import (
    InvalidCredentialsError,
    OjvTimeoutError,
    OjvUpstreamChangedError,
    OjvWafError,
    SessionExpiredError,
)
from app.ojv.session import OjvSession
from app.proxy_billing import ProxyBillingExhaustedError

RUT = "11.111.111-1"
PASSWORD = "synthetic-password-never-log"
COOKIE = "synthetic-cookie-never-log"


def _result(user_agent: str) -> BrowserLoginResult:
    return BrowserLoginResult((CookieRecord(
        name="AUTH", value=COOKIE, domain="oficinajudicialvirtual.pjud.cl", secure=True,
    ),), user_agent=user_agent)


async def test_login_uses_only_the_injected_official_adapter_and_replaces_cookie_jar() -> None:
    calls = 0

    async def login(*_args: object, user_agent: str, **_kwargs: object) -> BrowserLoginResult:
        nonlocal calls
        calls += 1
        return _result(user_agent)

    session = OjvSession(
        cookies={"F5": "borrowed-public-cookie"}, rate_limit_s=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
        browser_login=login,
    )
    await session.login(SecretStr(RUT), SecretStr(PASSWORD))
    assert calls == 1
    assert session.authenticated_form_identity() == ("11111111", "1")
    assert session._client.cookies.get("AUTH") == COOKIE
    assert session._client.cookies.get("F5") is None
    await session.close()


async def test_independent_sessions_do_not_share_authenticated_cookie_jars() -> None:
    async def login(*_args: object, user_agent: str, **_kwargs: object) -> BrowserLoginResult:
        value = "first" if user_agent == "one" else "second"
        return BrowserLoginResult((CookieRecord(
            name="AUTH", value=value, domain="oficinajudicialvirtual.pjud.cl", secure=True,
        ),), user_agent=user_agent)

    one = OjvSession(cookies={"PUBLIC": "one"}, user_agent="one", browser_login=login)
    two = OjvSession(cookies={"PUBLIC": "two"}, user_agent="two", browser_login=login)
    await one.login(SecretStr(RUT), SecretStr(PASSWORD))
    await two.login(SecretStr(RUT), SecretStr(PASSWORD))
    assert one._client.cookies.get("AUTH") == "first"
    assert two._client.cookies.get("AUTH") == "second"
    assert one._client.cookies.get("PUBLIC") is None
    assert two._client.cookies.get("PUBLIC") is None
    await one.close()
    await two.close()


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (InvalidCredentialsError, "credential_invalid"), (OjvWafError, "waf"),
        (OjvTimeoutError, "timeout"), (OjvUpstreamChangedError, "upstream_changed"),
        (SessionExpiredError, "session_expired"), (ProxyBillingExhaustedError, None),
    ],
)
async def test_login_preserves_closed_adapter_errors_and_clears_borrowed_state(
    error_type: type[BaseException], code: str | None,
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> BrowserLoginResult:
        raise error_type()

    session = OjvSession(
        cookies={"F5": COOKIE}, rate_limit_s=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
        browser_login=fail,
    )
    with pytest.raises(error_type) as exc_info:
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))
    if code is not None:
        assert getattr(exc_info.value, "code").value == code
    assert list(session._client.cookies.jar) == []
    with pytest.raises(SessionExpiredError):
        session.authenticated_form_identity()
    await session.close()


async def test_login_has_no_retry_when_adapter_times_out() -> None:
    calls = 0

    async def fail(*_args: object, **_kwargs: object) -> BrowserLoginResult:
        nonlocal calls
        calls += 1
        raise OjvTimeoutError()

    session = OjvSession(rate_limit_s=0, browser_login=fail)
    with pytest.raises(OjvTimeoutError):
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))
    assert calls == 1
    await session.close()


async def test_login_propagates_cancellation_after_clearing_borrowed_state() -> None:
    async def cancel(*_args: object, **_kwargs: object) -> BrowserLoginResult:
        raise asyncio.CancelledError()

    session = OjvSession(cookies={"F5": COOKIE}, rate_limit_s=0, browser_login=cancel)
    with pytest.raises(asyncio.CancelledError):
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))
    assert list(session._client.cookies.jar) == []
    await session.close()


async def test_session_requires_secret_wrappers_at_the_auth_boundary() -> None:
    session = OjvSession(rate_limit_s=0)
    with pytest.raises(TypeError, match="SecretStr"):
        await session.login(RUT, PASSWORD)  # type: ignore[arg-type]
    await session.close()


async def test_close_is_idempotent_and_clears_identity_cookies_and_transport() -> None:
    session = OjvSession(rate_limit_s=0, cookies={"OJVID": COOKIE})
    session._remember_authenticated_rut(SecretStr(RUT))
    await session.close()
    await session.close()
    assert session._client.is_closed is True
    assert list(session._client.cookies.jar) == []
    with pytest.raises(SessionExpiredError):
        session.authenticated_form_identity()


async def test_session_does_not_log_sensitive_adapter_success(caplog: pytest.LogCaptureFixture) -> None:
    async def login(*_args: object, user_agent: str, **_kwargs: object) -> BrowserLoginResult:
        return _result(user_agent)

    session = OjvSession(rate_limit_s=0, browser_login=login)
    with caplog.at_level(logging.INFO):
        await session.login(SecretStr(RUT), SecretStr(PASSWORD))
    rendered = f"{session!r} {vars(session)!r} {caplog.text}"
    assert RUT not in rendered
    assert "11111111" not in rendered
    assert PASSWORD not in rendered
    assert COOKIE not in rendered
    await session.close()
