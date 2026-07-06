import logging
from dataclasses import dataclass

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_CONSULTA_PATH = "/consultaUnificada.php"
# Selector del formulario real; su presencia = challenge superado.
_FORM_READY_SELECTOR = "select#competencia, select[name='competencia']"
_MINT_TIMEOUT_MS = 30_000


@dataclass
class MintResult:
    cookies: dict[str, str]
    user_agent: str


def cookies_to_dict(pw_cookies: list[dict]) -> dict[str, str]:
    return {c["name"]: c["value"] for c in pw_cookies}


class CookieMinter:
    """Lanza Chromium headless, resuelve el challenge F5 y devuelve cookies TSPD + UA.

    Launch-on-demand: no mantiene el browser vivo. Cleanup garantizado.
    """

    def __init__(self, base_url: str, proxy: str | None = None):
        self._base_url = base_url.rstrip("/")
        self._proxy = proxy

    async def mint(self) -> MintResult:
        launch_kwargs = {"headless": True}
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(
                    f"{self._base_url}{_CONSULTA_PATH}",
                    wait_until="domcontentloaded",
                    timeout=_MINT_TIMEOUT_MS,
                )
                await page.wait_for_selector(_FORM_READY_SELECTOR, timeout=_MINT_TIMEOUT_MS)
                ua = await page.evaluate("() => navigator.userAgent")
                pw_cookies = await context.cookies()
                cookies = cookies_to_dict(pw_cookies)
                if "TSPD_101" not in cookies:
                    raise RuntimeError("Minteo sin TSPD_101 — challenge no superado")
                logger.info("Cookies minteados (TSPD_101 presente), UA=%s", ua[:40])
                return MintResult(cookies=cookies, user_agent=ua)
            finally:
                await browser.close()
