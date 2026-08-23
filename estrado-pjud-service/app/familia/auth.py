"""Familia-specific operations over the shared authenticated OJV session."""

from __future__ import annotations

from pydantic import SecretStr

from app.ojv.errors import (
    FamiliaBlockedError,
    InvalidCredentialsError,
    OjvSessionError,
    OjvTimeoutError,
    OjvUpstreamChangedError,
    SessionError,
    SessionExpiredError,
)
from app.ojv.session import (
    OjvSession,
    decode_ojv_html,
    detect_login_error,
    looks_like_login_url,
    rut_parts,
)
from app.parsers.search_parser import detect_blocked


_OJV_BASE = "https://oficinajudicialvirtual.pjud.cl"
_FAMILIA_SEARCH = f"{_OJV_BASE}/misCausas/familia/consultaMisCausasFamilia.php"
_FAMILIA_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{_OJV_BASE}/consultaUnificada.php",
}

# Compatibility names retained while callers migrate to ``app.ojv``.
_decode = decode_ojv_html
_detect_login_error = detect_login_error
_detect_session_error = looks_like_login_url
_rut_parts = rut_parts


class FamiliaAuthSession(OjvSession):
    """Shared OJV session plus Familia's observed listing form."""

    async def search_familia(
        self,
        rut: SecretStr,
        rit: str = "",
        year: str = "",
    ) -> str:
        if not isinstance(rut, SecretStr):
            raise TypeError("rut must be SecretStr")
        rut_digits, dv = rut_parts(rut.get_secret_value())
        form_data = {
            "rutMisCauFam": rut_digits[:8],
            "dvMisCauFam": dv,
            "tipoMisCauFam": "0",
            "rolMisCauFam": rit,
            "anhoMisCauFam": year,
            "tipCausaMisCauFam[]": "M",
            "estadoCausaMisCauFam[]": "1",
            "fecDesdeMisCauFam": "",
            "fecHastaMisCauFam": "",
            "nombreMisCauFam": "",
            "apePatMisCauFam": "",
            "apeMatMisCauFam": "",
        }
        try:
            response = await self.post_form(
                _FAMILIA_SEARCH,
                list(form_data.items()),
                headers={
                    **_FAMILIA_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        finally:
            form_data.clear()
            rut_digits = ""
            dv = ""
        html = decode_ojv_html(response)
        search_error: OjvSessionError | None = None
        if response.status_code in {403, 429} or detect_blocked(html):
            search_error = FamiliaBlockedError()
        elif response.status_code in {401, 419} or looks_like_login_url(str(response.url)):
            search_error = SessionExpiredError()
        elif response.status_code == 408 or response.status_code >= 500:
            search_error = OjvTimeoutError()
        elif response.status_code >= 400:
            search_error = OjvUpstreamChangedError()
        response = None
        if search_error is not None:
            html = ""
            raise search_error
        return html


__all__ = [
    "FamiliaAuthSession",
    "FamiliaBlockedError",
    "InvalidCredentialsError",
    "OjvSession",
    "SessionError",
    "SessionExpiredError",
    "decode_ojv_html",
]
