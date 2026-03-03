import logging
import re

from app.adapters.http_adapter import OJVHttpAdapter

logger = logging.getLogger(__name__)

_CSRF_RE = re.compile(r"token:\s*'([a-f0-9]{32})'")

_AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://oficinajudicialvirtual.pjud.cl/indexN.php",
}


class OJVSession:
    """Manages a single OJV session: cookies + CSRF token."""

    def __init__(self, adapter: OJVHttpAdapter):
        self._adapter = adapter
        self.csrf_token: str | None = None

    async def initialize(self):
        """Step 1+2: Load initial page for cookies + CSRF, then activate guest session."""
        # Step 1: GET main page to get cookies + CSRF
        resp = await self._adapter.get("/consultaUnificada.php")
        html = resp.content.decode("latin-1")

        m = _CSRF_RE.search(html)
        if m:
            self.csrf_token = m.group(1)
            logger.info("CSRF token acquired: %s...", self.csrf_token[:8])
        else:
            logger.warning("CSRF token not found in initial page")

        # Step 2: Activate guest session
        await self._adapter.post(
            "/includes/sesion-invitado.php",
            headers=_AJAX_HEADERS,
        )
        logger.info("Guest session activated")

    async def search(self, competencia_path: str, form_data: dict) -> str:
        """Step 3: POST search and return HTML (decoded latin-1)."""
        path = f"/ADIR_871/{competencia_path}/consultaRit{competencia_path.capitalize()}.php"
        resp = await self._adapter.post(
            path,
            data=form_data,
            headers={
                **_AJAX_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        return resp.content.decode("latin-1")

    async def detail(self, competencia_path: str, jwt_token: str) -> str:
        """Step 4: POST detail request and return HTML (decoded latin-1)."""
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
        return resp.content.decode("latin-1")

    async def close(self):
        await self._adapter.close()
