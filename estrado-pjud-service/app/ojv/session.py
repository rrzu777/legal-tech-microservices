"""Short-lived authenticated session shared by private OJV flows."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlparse

import httpx
from pydantic import SecretStr

from app.adapters.http_adapter import _USER_AGENT
from app.bandwidth import estimate_request_bytes, record_proxy_request, record_proxy_response
from app.cookie_scope import (
    CookieRecord,
    cookie_jar_from_records,
    legacy_cookie_records,
    legacy_cookie_scope,
)
from app.ojv.errors import (
    OjvTimeoutError,
    OjvUpstreamChangedError,
    SessionError,
    SessionExpiredError,
)
from app.proxy_billing import ProxyBillingExhaustedError, is_proxy_billing_error


logger = logging.getLogger(__name__)

_OJV_BASE = "https://oficinajudicialvirtual.pjud.cl"

BrowserLoginCallable = Callable[..., Awaitable[object]]


def decode_ojv_html(response: httpx.Response) -> str:
    """Decode only the two encodings observed on authenticated OJV pages."""
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.content.decode("latin-1")


def rut_parts(rut: str) -> tuple[str, str]:
    normalized = rut.replace(".", "").strip().upper()
    if "-" in normalized:
        body, dv = normalized.rsplit("-", 1)
        return body[:8], dv[:1]
    clean = normalized.replace("-", "")
    if len(clean) >= 9:
        return clean[:-1][:8], clean[-1]
    return clean[:8], ""


def detect_login_error(html: str) -> bool:
    lower = html.lower()
    return any(
        marker in lower
        for marker in (
            "gob-response-error",
            "clave incorrecta",
            "rut o clave",
            "rut o contraseña",
            "rut o constraseña",
            "credenciales inválidas",
            "no existe",
            "contraseña incorrecta",
            "rut incorrecto",
            "usuario no encontrado",
            "clave poder judicial incorrecta",
            "rut no registrado",
        )
    )


def looks_like_login_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    return host == "accounts.claveunica.gob.cl" or "login" in path or "error" in path


class OjvSession:
    """One authenticated OJV cookie jar with explicit sensitive-state lifetime."""

    def __init__(
        self,
        proxy_url: str | None = None,
        cookies: Sequence[CookieRecord] | Mapping[str, str] | None = None,
        user_agent: str | None = None,
        rate_limit_s: float = 2.5,
        transport: httpx.AsyncBaseTransport | None = None,
        browser_login: BrowserLoginCallable | None = None,
    ) -> None:
        self._rate_s = rate_limit_s
        self._last = 0.0
        self._authenticated_rut: SecretStr | None = None
        self._authenticated_dv: SecretStr | None = None
        self._closed = False
        self._browser_proxy = SecretStr(proxy_url) if proxy_url else None
        self._user_agent = user_agent or _USER_AGENT
        self._browser_login = browser_login
        cookie_records = cookies or ()
        if isinstance(cookie_records, Mapping):
            domain, secure = legacy_cookie_scope(_OJV_BASE)
            cookie_records = legacy_cookie_records(cookie_records, domain=domain, secure=secure)
        self._client = httpx.AsyncClient(
            proxy=proxy_url,
            transport=transport,
            cookies=cookie_jar_from_records(cookie_records),
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={
                "User-Agent": self._user_agent,
                "Accept-Language": "es-CL,es;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(closed={self._closed}, authenticated={self._authenticated_rut is not None})"

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        record_proxy_request(estimate_request_bytes(kwargs))
        transport_failed = False
        billing_failed = False
        response: httpx.Response | None = None
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TransportError as error:
            billing_failed = is_proxy_billing_error(error)
            transport_failed = not billing_failed
        if billing_failed:
            kwargs.clear()
            raise ProxyBillingExhaustedError()
        if transport_failed:
            # Raise outside the handler: upstream exceptions can retain request
            # bodies, so even an explicitly suppressed chain is too much state.
            kwargs.clear()
            raise OjvTimeoutError()
        if response is None:  # defensive: the HTTP client returns or raises
            kwargs.clear()
            raise OjvUpstreamChangedError()
        record_proxy_response(len(response.content))
        kwargs.clear()
        return response

    async def _get(self, url: str, **kwargs: object) -> httpx.Response:
        try:
            return await self._request("GET", url, **kwargs)
        finally:
            kwargs.clear()

    async def _post(self, url: str, **kwargs: object) -> httpx.Response:
        try:
            return await self._request("POST", url, **kwargs)
        finally:
            kwargs.clear()

    async def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._rate_s:
            await asyncio.sleep(self._rate_s - elapsed)
        self._last = time.monotonic()

    async def post_form(
        self,
        url: str,
        data: Sequence[tuple[str, str]],
        *,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        await self._wait()
        form_items = list(data)
        data = ()
        encoded = urlencode(form_items)
        form_items.clear()
        try:
            return await self._post(url, content=encoded, headers=headers)
        finally:
            encoded = ""

    @staticmethod
    def _require_secret(value: object, field: str) -> SecretStr:
        if not isinstance(value, SecretStr):
            raise TypeError(f"{field} must be SecretStr")
        return value

    async def login(
        self,
        rut: SecretStr,
        password: SecretStr,
        auth_type: str = "clave_pj",
    ) -> None:
        rut_secret = self._require_secret(rut, "rut")
        password_secret = self._require_secret(password, "password")
        self._authenticated_rut = None
        self._authenticated_dv = None
        if auth_type != "clave_pj":
            raise ValueError("Only Clave Poder Judicial is supported")

        from app.ojv.browser_login import login_official_ojv

        login_callable = self._browser_login or login_official_ojv
        proxy_value = (
            self._browser_proxy.get_secret_value()
            if self._browser_proxy is not None
            else None
        )
        result: object | None = None
        cookies: object | None = None
        new_jar = None
        try:
            result = await login_callable(
                rut_secret,
                password_secret,
                proxy_url=proxy_value,
                user_agent=self._user_agent,
            )
            cookies = getattr(result, "cookies", None)
            result_user_agent = getattr(result, "user_agent", None)
            if result_user_agent != self._user_agent:
                raise OjvUpstreamChangedError()
            new_jar = cookie_jar_from_records(cookies)
        except BaseException:
            # A failed browser attempt must never leave a borrowed/public jar
            # appearing authenticated to the bounded listing client.
            self._client.cookies.clear()
            self._authenticated_rut = None
            self._authenticated_dv = None
            proxy_value = None
            cookies = None
            new_jar = None
            result = None
            raise
        proxy_value = None
        cookies = None
        result = None
        try:
            self._client.cookies.clear()
            for cookie in new_jar:
                self._client.cookies.jar.set_cookie(cookie)
            self._remember_authenticated_rut(rut_secret)
        except BaseException:
            self._client.cookies.clear()
            self._authenticated_rut = None
            self._authenticated_dv = None
            raise
        finally:
            new_jar = None
        logger.info("ojv_session login status=ok")

    def _remember_authenticated_rut(self, rut: SecretStr) -> None:
        rut_secret = self._require_secret(rut, "rut")
        rut_digits, dv = rut_parts(rut_secret.get_secret_value())
        if not rut_digits.isdigit() or not dv or (not dv.isdigit() and dv != "K"):
            return
        self._authenticated_rut = SecretStr(rut_digits)
        self._authenticated_dv = SecretStr(dv)

    def authenticated_form_identity(self) -> tuple[str, str]:
        if self._authenticated_rut is None or self._authenticated_dv is None:
            raise SessionError()
        return (
            self._authenticated_rut.get_secret_value(),
            self._authenticated_dv.get_secret_value(),
        )

    async def close(self) -> None:
        self._authenticated_rut = None
        self._authenticated_dv = None
        self._client.cookies.clear()
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    async def __aenter__(self) -> "OjvSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


async def open_ojv_session(rut: SecretStr, password: SecretStr) -> OjvSession:
    """Open the default authenticated session, closing it if login fails."""
    session = OjvSession()
    try:
        await session.login(rut=rut, password=password)
    except BaseException:
        await session.close()
        raise
    return session
