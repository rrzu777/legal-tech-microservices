"""Authenticated OJV session for Familia — supports Clave PJ and Clave Única login."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from bs4 import BeautifulSoup

from app.adapters.http_adapter import _USER_AGENT

logger = logging.getLogger(__name__)

_OJV_BASE   = "https://oficinajudicialvirtual.pjud.cl"
_OJV_KPITEC = "https://ojv.pjud.cl"
_CU_BASE    = "https://accounts.claveunica.gob.cl"

_CPJ_LOGIN_PAGE = f"{_OJV_KPITEC}/kpitec-ojv-web/views/login_pjud.html"
_CPJ_LOGIN_API  = f"{_OJV_KPITEC}/kpitec-ojv-web/login_pjud"

_CU_HOME     = f"{_OJV_BASE}/home/index.php"
_CU_INIT_URL = f"{_OJV_BASE}/home/initCU.php"

# SHA1 field name in cuform on home/index.php — CONSTANT (verified 2026-04-20)
_CU_FORM_FIELD = "2257e205d71edbaab04591f61be0066f5582d591"

_FAMILIA_SEARCH = f"{_OJV_BASE}/misCausas/familia/consultaMisCausasFamilia.php"

_FAMILIA_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{_OJV_BASE}/consultaUnificada.php",
}


def _decode(resp: httpx.Response) -> str:
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        text = resp.content.decode("latin-1")
        try:
            return text.encode("latin-1").decode("utf-8", errors="replace")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text


def _rut_parts(rut: str) -> tuple[str, str]:
    """Return (digits_only_8, dv) from any RUT format."""
    clean = rut.replace(".", "").replace("-", "").strip()
    if len(clean) >= 9:
        return clean[:-1][:8], clean[-1]
    return clean[:8], ""


def _extract_cu_login_params(html: str) -> tuple[str | None, str | None]:
    """Parse CU login page once; return (csrf_token, next_value)."""
    soup = BeautifulSoup(html, "html.parser")
    csrf_inp = soup.find("input", {"name": "csrfmiddlewaretoken"})
    next_inp = soup.find("input", {"name": "next"})
    return (
        csrf_inp.get("value") if csrf_inp else None,
        next_inp.get("value") if next_inp else None,
    )


def _detect_login_error(html: str) -> bool:
    lower = html.lower()
    return any(k in lower for k in [
        "gob-response-error", "clave incorrecta", "rut o clave",
        "credenciales inválidas", "no existe", "contraseña incorrecta",
        "rut incorrecto", "usuario no encontrado",
        "clave poder judicial incorrecta", "rut no registrado",
    ])


def _detect_session_ok(url: str) -> bool:
    return any(k in url for k in [
        "indexN.php", "/home/index", "consultaUnificada", "/misCausas",
    ])


class FamiliaAuthSession:
    """Short-lived authenticated OJV session for a single Familia sync."""

    def __init__(self, rate_limit_s: float = 2.5):
        self._rate_s = rate_limit_s
        self._last: float = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "es-CL,es;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    async def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._rate_s:
            await asyncio.sleep(self._rate_s - elapsed)
        self._last = time.monotonic()

    async def _login_clave_pj(self, rut: str, password: str) -> None:
        rut_digits, _ = _rut_parts(rut)

        await self._wait()
        await self._client.get(_CPJ_LOGIN_PAGE)  # obtain F5 BIG-IP cookies

        await self._wait()
        resp = await self._client.post(
            _CPJ_LOGIN_API,
            data={"rutPjud": rut_digits, "passwordPjud": password},
            headers={"Referer": _CPJ_LOGIN_PAGE, "Content-Type": "application/x-www-form-urlencoded"},
        )
        html = _decode(resp)
        final_url = str(resp.url)

        if _detect_login_error(html):
            raise InvalidCredentialsError("Clave PJ: credentials rejected")
        if not _detect_session_ok(final_url):
            logger.warning("Clave PJ login: unexpected final URL %s", final_url[:80])
            raise SessionError(f"Clave PJ: unexpected redirect to {final_url[:80]}")

        logger.info("Clave PJ session established")

    async def _login_clave_unica(self, rut: str, password: str) -> None:
        rut_digits, _ = _rut_parts(rut)

        await self._wait()
        resp_home = await self._client.get(_CU_HOME)
        soup = BeautifulSoup(_decode(resp_home), "html.parser")
        cuform = soup.find("form", {"id": "cuform"})
        if not cuform:
            raise SessionError("Clave Única: cuform not found on home/index.php")
        inp = cuform.find("input")
        if not inp:
            raise SessionError("Clave Única: cuform has no hidden input")

        field_name = inp.get("name", "")
        jwt_value  = inp.get("value", "")

        if field_name != _CU_FORM_FIELD:
            logger.warning(
                "Clave Única: cuform field name changed from %s to %s — update _CU_FORM_FIELD",
                _CU_FORM_FIELD, field_name,
            )

        await self._wait()
        resp_cu = await self._client.post(
            _CU_INIT_URL,
            data={field_name: jwt_value},
            headers={"Referer": _CU_HOME},
        )
        html_cu = _decode(resp_cu)
        cu_url  = str(resp_cu.url)

        if _CU_BASE not in cu_url:
            raise SessionError(f"Clave Única: expected CU login page, got {cu_url[:80]}")

        csrf, next_val = _extract_cu_login_params(html_cu)
        if not csrf:
            raise SessionError("Clave Única: CSRF token not found on login page")

        # token="" is intentional — reCAPTCHA v3 present but not enforced server-side (verified 2026-04-20)
        await self._wait()
        resp_login = await self._client.post(
            cu_url,
            data={
                "csrfmiddlewaretoken": csrf,
                "next":                next_val or "",
                "app_name":            "PJUD",
                "token":               "",
                "run":                 rut_digits,
                "password":            password,
            },
            headers={"Referer": cu_url, "Origin": _CU_BASE, "Content-Type": "application/x-www-form-urlencoded"},
        )
        html_login = _decode(resp_login)
        final_url  = str(resp_login.url)

        if _detect_login_error(html_login):
            raise InvalidCredentialsError("Clave Única: credentials rejected")
        if not _detect_session_ok(final_url):
            logger.warning("Clave Única login: unexpected final URL %s", final_url[:80])
            raise SessionError(f"Clave Única: unexpected redirect to {final_url[:80]}")

        logger.info("Clave Única session established via %s", final_url[:60])

    async def login(self, rut: str, password: str, auth_type: str) -> None:
        if auth_type == "clave_pj":
            await self._login_clave_pj(rut, password)
        elif auth_type == "clave_unica":
            await self._login_clave_unica(rut, password)
        else:
            raise ValueError(f"Unknown auth_type: {auth_type!r}")

    async def search_familia(self, rut: str, rit: str = "", year: str = "") -> str:
        rut_digits, dv = _rut_parts(rut)
        form_data: dict[str, str] = {
            "rutMisCauFam":           rut_digits[:8],
            "dvMisCauFam":            dv,
            "tipoMisCauFam":          "0",
            "rolMisCauFam":           rit,
            "anhoMisCauFam":          year,
            "tipCausaMisCauFam[]":    "M",
            "estadoCausaMisCauFam[]": "1",
            "fecDesdeMisCauFam":      "",
            "fecHastaMisCauFam":      "",
            "nombreMisCauFam":        "",
            "apePatMisCauFam":        "",
            "apeMatMisCauFam":        "",
        }
        await self._wait()
        resp = await self._client.post(
            _FAMILIA_SEARCH,
            data=form_data,
            headers={**_FAMILIA_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return _decode(resp)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FamiliaAuthSession":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()


class InvalidCredentialsError(Exception):
    """OJV/CU rejected the provided credentials."""


class SessionError(Exception):
    """Failed to establish an authenticated OJV session."""
