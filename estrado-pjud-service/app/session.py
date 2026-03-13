import logging
import re
import time

import httpx

from app.adapters.http_adapter import OJVHttpAdapter

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
