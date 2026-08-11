import logging
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError, async_playwright

from app.bandwidth import record_proxy_request, record_proxy_response
from app.failure_kind import MintUnavailableError
from app.proxy import split_proxy_for_playwright

logger = logging.getLogger(__name__)

_CONSULTA_PATH = "/consultaUnificada.php"
# Selector del formulario real; su presencia = challenge superado.
_FORM_READY_SELECTOR = "select#competencia, select[name='competencia']"
_MINT_TIMEOUT_MS = 30_000

# El challenge JS de F5 NO se resuelve en un browser headless ni en uno con
# el flag de automatización visible. Verificado empíricamente (6 jul 2026):
# solo la combinación headed + este arg mintea TSPD_101; cualquier variante
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
    cookies: dict[str, str] = {}
    scopes: dict[str, tuple[str, str, str]] = {}
    for cookie in pw_cookies:
        name = cookie["name"]
        scope = (cookie["value"], cookie.get("domain", ""), cookie.get("path", ""))
        previous_scope = scopes.get(name)
        if previous_scope is not None and previous_scope != scope:
            raise ValueError("ambiguous_cookie_scope")
        scopes[name] = scope
        cookies[name] = cookie["value"]
    return cookies


class CookieMinter:
    """Lanza Chromium (headed, bajo Xvfb en el VPS), resuelve el challenge F5
    y devuelve cookies TSPD + UA.

    Launch-on-demand: no mantiene el browser vivo. Cleanup garantizado.
    Corre headed a propósito: el challenge anti-bot de F5 no se resuelve headless.
    """

    def __init__(self, base_url: str, proxy: str | None = None):
        self._base_url = base_url.rstrip("/")
        self._proxy = proxy

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
                logger.info(
                    "PJUD form ready; cookie_count=%d has_php_session=%s has_ts_family=%s",
                    len(cookies), "PHPSESSID" in cookies,
                    any(name.startswith("TS") for name in cookies),
                )
                return MintResult(cookies=cookies, user_agent=ua)
            finally:
                if page is not None:
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
                                # Test/mocked browsers may not emit request events.
                                # The fallback still makes a paid navigation durable.
                                if observed_requests == 0:
                                    record_proxy_request(0)
                                record_proxy_response(
                                    int(transfer.get("transferSize") or 0)
                                )
                    except Exception:
                        logger.warning("pjud_mint_transfer_telemetry_unavailable")
                try:
                    await browser.close()
                except PlaywrightError:
                    logger.warning("pjud_mint_browser_cleanup_unavailable")
