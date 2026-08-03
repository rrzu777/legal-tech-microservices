import logging
import re
import time

import httpx

from app.adapters.http_adapter import OJVHttpAdapter
from app.failure_kind import (
    BlockedInitialPageError,
    MissingCsrfTokenError,
    reject_empty_body,
)
from app.parsers.search_parser import detect_blocked

logger = logging.getLogger(__name__)

_CSRF_RE = re.compile(r"token:\s*'([a-f0-9]{32})'")


def _decode(resp: httpx.Response) -> str:
    """Decode response handling PJUD's mixed encoding.

    PJUD responses may mix UTF-8 and latin-1 bytes in the same document.
    Strategy: try UTF-8 first (clean responses), fall back to latin-1
    then repair common UTF-8 mojibake patterns (e.g. Ã³ -> ó).
    """
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        text = resp.content.decode("latin-1")
        # Repair UTF-8 bytes that were decoded as latin-1 (mojibake)
        try:
            return text.encode("latin-1").decode("utf-8", errors="replace")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text


_AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://oficinajudicialvirtual.pjud.cl/consultaUnificada.php",
}


class OJVSession:
    """Manages a single OJV session: cookies + CSRF token."""

    def __init__(self, adapter: OJVHttpAdapter):
        self._adapter = adapter
        self.csrf_token: str | None = None
        self._created_at: float = time.monotonic()

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self._created_at

    async def initialize(self):
        """Step 1+2: Load initial page for cookies + CSRF, then activate guest session."""
        # Step 1: GET main page to get cookies + CSRF
        # Must use consultaUnificada.php (not indexN.php) because only
        # this page contains the CSRF token needed for detail requests.
        resp = await self._adapter.get("/consultaUnificada.php")
        resp.raise_for_status()
        html = _decode(resp)

        # Cero bytes ANTES del detector de bloqueo, como en los otros cuatro
        # call sites: `detect_blocked("")` devuelve True, así que sin esta línea
        # el túnel cortándose se reportaría como challenge de F5 — la conflación
        # exacta contra la que advierte `EmptyResponseError`, y que sostuvo el
        # outage de dos meses y medio.
        reject_empty_body(html, "pagina inicial")

        # El challenge de F5 sale con HTTP 200, así que `raise_for_status()` lo
        # deja pasar: si no se mira el CUERPO, una sesión bloqueada sigue de
        # largo y el bloqueo recién aparece en el POST de abajo, disfrazado de
        # error de transporte. Ver `BlockedInitialPageError`.
        if detect_blocked(html):
            raise BlockedInitialPageError(
                f"la página inicial vino bloqueada ({len(resp.content)} bytes)"
            )

        # El largo se loguea SIEMPRE, no sólo al fallar: la diferencia entre la
        # página buena y el challenge son 186.008 bytes contra 4.901, y no
        # tenerlo escrito es lo que obligó a reconstruir el incidente a partir
        # de diez días de journal.
        logger.info("Página inicial OK (%d bytes)", len(resp.content))

        m = _CSRF_RE.search(html)
        if m:
            self.csrf_token = m.group(1)
            logger.info("CSRF token acquired: %s...", self.csrf_token[:8])
        else:
            logger.warning("CSRF token not found in initial page")

        # Step 2: Activate guest session
        resp = await self._adapter.post(
            "/includes/sesion-invitado.php",
            headers=_AJAX_HEADERS,
        )
        resp.raise_for_status()
        logger.info("Guest session activated")

    async def search(self, competencia_path: str, form_data: dict) -> str:
        """Step 3: POST search and return decoded HTML."""
        path = f"/ADIR_871/{competencia_path}/consultaRit{competencia_path.capitalize()}.php"
        resp = await self._adapter.post(
            path,
            data=form_data,
            headers={
                **_AJAX_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        return _decode(resp)

    async def detail(self, competencia_path: str, jwt_token: str) -> str:
        """Step 4: POST detail request and return decoded HTML."""
        if not self.csrf_token:
            # Sin token OJV contesta 405 con cero bytes — medido, ver
            # `MissingCsrfTokenError`. Salir igual no consigue nada y encima
            # gasta reputación de la IP residencial para cobrar un 405 seguro.
            # Ahorra 1 de los 3 requests del intento, no los 3: `initialize()`
            # ya gastó el GET de la página y el POST de sesión-invitado.
            raise MissingCsrfTokenError(
                f"sin token CSRF para el detalle de {competencia_path} "
                "(el regex no matcheó en consultaUnificada.php)"
            )
        path = f"/ADIR_871/{competencia_path}/modal/causa{competencia_path.capitalize()}.php"
        resp = await self._adapter.post(
            path,
            data={
                "dtaCausa": jwt_token,
                "token": self.csrf_token,
            },
            headers={
                **_AJAX_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://oficinajudicialvirtual.pjud.cl",
            },
        )
        logger.info(
            "Detail response: comp=%s status=%d length=%d",
            competencia_path, resp.status_code, len(resp.content),
        )
        resp.raise_for_status()
        return _decode(resp)

    async def fetch_anexo_list(self, endpoint: str, param: str, jwt: str) -> str:
        """POST to an anexo modal endpoint. Returns HTML with document table.

        No CSRF needed — PJUD's JS sends only the JWT param.
        """
        resp = await self._adapter.post(
            f"/ADIR_871/{endpoint}",
            data={param: jwt},
            headers=_AJAX_HEADERS,
        )
        resp.raise_for_status()
        return _decode(resp)

    async def download_document(self, path: str, token: str, param_name: str = "dtaDoc") -> httpx.Response:
        """Download a document from PJUD using the form action path + token."""
        resp = await self._adapter.get(
            f"/{path}",
            params={param_name: token},
        )
        resp.raise_for_status()
        return resp

    async def close(self):
        await self._adapter.close()
