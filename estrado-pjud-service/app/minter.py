import asyncio
import logging
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError, async_playwright

from app.bandwidth import record_proxy_request, record_proxy_response
from app.cookie_scope import flatten_cookie_name_values
from app.failure_kind import MintUnavailableError
from app.proxy import split_proxy_for_playwright

logger = logging.getLogger(__name__)

_CONSULTA_PATH = "/consultaUnificada.php"
# Selector del formulario real; su presencia = challenge superado.
_FORM_READY_SELECTOR = "select#competencia, select[name='competencia']"
_MINT_TIMEOUT_MS = 30_000
_CLEANUP_TIMEOUT_S = 1.0

# El challenge JS de F5 NO se resuelve en un browser headless ni en uno con
# el flag de automatización visible. Verificado empíricamente (6 jul 2026):
# solo la combinación headed + este arg supera el challenge; cualquier variante
# headless deja el challenge en loop. En el VPS (sin monitor) esto corre
# dentro de Xvfb (display virtual). Ver spec §3.1 y §9.
_ANTIBOT_ARGS = [
    "--disable-blink-features=AutomationControlled",
    # Requeridos al correr bajo el servicio systemd (User=estrado no-root,
    # NoNewPrivileges, PrivateTmp): Chromium no puede usar su sandbox setuid
    # (--no-sandbox) y /dev/shm está restringido (--disable-dev-shm-usage).
    # No afectan la resolución del challenge (son de aislamiento de proceso).
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


@dataclass
class MintResult:
    cookies: dict[str, str]
    user_agent: str


def cookies_to_dict(pw_cookies: list[dict]) -> dict[str, str]:
    return flatten_cookie_name_values(
        (cookie["name"], cookie["value"]) for cookie in pw_cookies
    )


class CookieMinter:
    """Lanza Chromium (headed, bajo Xvfb en el VPS), resuelve el challenge F5
    y devuelve cookies + UA.

    Launch-on-demand: no mantiene el browser vivo. Cleanup garantizado.
    Corre headed a propósito: el challenge anti-bot de F5 no se resuelve headless.
    """

    def __init__(self, base_url: str, proxy: str | None = None):
        self._base_url = base_url.rstrip("/")
        self._proxy = proxy

    async def _close_playwright_resource(self, resource, label: str) -> None:
        """Close a browser resource without allowing cleanup to extend PJUD traffic."""
        close = getattr(resource, "close", None)
        if close is None:
            return
        try:
            await asyncio.wait_for(close(), timeout=_CLEANUP_TIMEOUT_S)
        except (asyncio.TimeoutError, PlaywrightError):
            logger.warning("pjud_mint_%s_cleanup_unavailable", label)

    async def mint(self) -> MintResult:
        launch_kwargs = {"headless": False, "args": _ANTIBOT_ARGS}
        if self._proxy:
            # Playwright rechaza credenciales embebidas en `server`
            # (net::ERR_INVALID_AUTH_CREDENTIALS, verificado en el VPS).
            # Deben ir separadas en username/password.
            launch_kwargs["proxy"] = split_proxy_for_playwright(self._proxy)

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(**launch_kwargs)
            except PlaywrightError:
                raise MintUnavailableError("browser_unavailable") from None
            context = None
            page = None
            observed_requests = 0

            def _record_started(request) -> None:
                nonlocal observed_requests
                observed_requests += 1
                try:
                    body = request.post_data_buffer or b""
                except Exception:
                    body = b""
                record_proxy_request(len(body))

            try:
                context = await browser.new_context()
                page = await context.new_page()
                # This fires as soon as Chromium starts a request, including
                # navigations that later timeout before performance data can be read.
                page.on("request", _record_started)
                try:
                    await page.goto(
                        f"{self._base_url}{_CONSULTA_PATH}",
                        wait_until="domcontentloaded",
                        timeout=_MINT_TIMEOUT_MS,
                    )
                except PlaywrightError:
                    raise MintUnavailableError("navigation_failed") from None
                try:
                    await page.wait_for_selector(
                        _FORM_READY_SELECTOR,
                        timeout=_MINT_TIMEOUT_MS,
                    )
                except PlaywrightError:
                    raise MintUnavailableError("form_timeout") from None
                ua = await page.evaluate("() => navigator.userAgent")
                pw_cookies = await context.cookies()
                cookies = cookies_to_dict(pw_cookies)
                try:
                    transfers = await page.evaluate(
                        """() => [
                          ...performance.getEntriesByType('navigation'),
                          ...performance.getEntriesByType('resource'),
                        ].map((entry) => ({
                          transferSize: entry.transferSize || entry.encodedBodySize || 0,
                        }))"""
                    )
                    if isinstance(transfers, list):
                        for transfer in transfers:
                            if not isinstance(transfer, dict):
                                continue
                            if observed_requests == 0:
                                record_proxy_request(0)
                            record_proxy_response(int(transfer.get("transferSize") or 0))
                except Exception:
                    logger.warning("pjud_mint_transfer_telemetry_unavailable")
                logger.info(
                    "PJUD form ready; cookie_count=%d has_php_session=%s has_ts_family=%s",
                    len(cookies), "PHPSESSID" in cookies,
                    any(name.startswith("TS") for name in cookies),
                )
                return MintResult(cookies=cookies, user_agent=ua)
            finally:
                # Do not inspect performance after a cancelled navigation: that
                # delays closing a live page and lets its in-flight requests keep
                # using the paid proxy past the acquisition deadline.
                # Closing the browser is Playwright's atomic shutdown for all
                # pages and contexts; starting there prevents a stuck child
                # close from postponing the network cutoff.
                await self._close_playwright_resource(browser, "browser")
