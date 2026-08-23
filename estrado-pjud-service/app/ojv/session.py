"""Short-lived authenticated session shared by private OJV flows."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
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
    FamiliaBlockedError,
    InvalidCredentialsError,
    OjvSessionError,
    OjvTimeoutError,
    OjvUpstreamChangedError,
    SessionError,
    SessionExpiredError,
)
from app.parsers.search_parser import detect_blocked
from app.proxy_billing import ProxyBillingExhaustedError, is_proxy_billing_error


logger = logging.getLogger(__name__)

_OJV_BASE = "https://oficinajudicialvirtual.pjud.cl"
_OJV_KPITEC = "https://ojv.pjud.cl"
_CPJ_LOGIN_PAGE = f"{_OJV_KPITEC}/kpitec-ojv-web/views/login_pjud.html"
_CPJ_LOGIN_API = f"{_OJV_KPITEC}/kpitec-ojv-web/login_pjud"


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
    ) -> None:
        self._rate_s = rate_limit_s
        self._last = 0.0
        self._authenticated_rut: SecretStr | None = None
        self._authenticated_dv: SecretStr | None = None
        self._closed = False
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
                "User-Agent": user_agent or _USER_AGENT,
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

        await self._wait()
        login_page = await self._get(_CPJ_LOGIN_PAGE)
        login_page_html = decode_ojv_html(login_page)
        login_page_error: OjvSessionError | None = None
        if login_page.status_code in {403, 429} or detect_blocked(login_page_html):
            login_page_error = FamiliaBlockedError()
        elif login_page.status_code == 408 or login_page.status_code >= 500:
            login_page_error = OjvTimeoutError()
        elif login_page.status_code >= 400:
            login_page_error = OjvUpstreamChangedError()
        login_page = None
        login_page_html = ""
        if login_page_error is not None:
            raise login_page_error

        rut_value = rut_secret.get_secret_value()
        password_value = password_secret.get_secret_value()
        rut_digits, _ = rut_parts(rut_value)

        await self._wait()
        credential_form = {"rutPjud": rut_digits, "passwordPjud": password_value}
        try:
            response = await self._post(
                _CPJ_LOGIN_API,
                data=credential_form,
                headers={
                    "Referer": _CPJ_LOGIN_PAGE,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        finally:
            credential_form.clear()
            rut_value = ""
            password_value = ""
            rut_digits = ""
        html = decode_ojv_html(response)
        login_error: OjvSessionError | None = None
        if response.status_code in {403, 429} or detect_blocked(html):
            login_error = FamiliaBlockedError()
        elif response.status_code == 419:
            login_error = SessionExpiredError()
        elif response.status_code == 408 or response.status_code >= 500:
            login_error = OjvTimeoutError()
        elif response.status_code == 401:
            login_error = InvalidCredentialsError()
        elif response.status_code >= 400:
            login_error = OjvUpstreamChangedError()
        elif detect_login_error(html):
            login_error = InvalidCredentialsError()
        elif looks_like_login_url(str(response.url)):
            login_error = SessionExpiredError()
        elif (response.url.host or "").casefold() != "oficinajudicialvirtual.pjud.cl":
            login_error = OjvUpstreamChangedError()
        response = None
        html = ""
        if login_error is not None:
            raise login_error

        self._remember_authenticated_rut(rut_secret)
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
