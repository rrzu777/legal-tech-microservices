import asyncio
import logging
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError, async_playwright

from app.bandwidth import record_proxy_request, record_proxy_response_increment
from app.cookie_scope import CookieRecord, legacy_cookie_records, playwright_cookie_records
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
    cookies: tuple[CookieRecord, ...]
    user_agent: str

    def __post_init__(self) -> None:
        if isinstance(self.cookies, dict):
            self.cookies = legacy_cookie_records(
                self.cookies,
                domain="oficinajudicialvirtual.pjud.cl",
                secure=True,
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

    async def _detach_cdp_session(self, session) -> None:
        try:
            await asyncio.wait_for(
                session.detach(),
                timeout=_CLEANUP_TIMEOUT_S,
            )
        except Exception:
            logger.warning("pjud_mint_cdp_cleanup_unavailable")

    async def _create_cdp_meter(self, context, page):
        session = None
        try:
            session = await asyncio.wait_for(
                context.new_cdp_session(page),
                timeout=_CLEANUP_TIMEOUT_S,
            )

            def _record_data_received(event) -> None:
                if not isinstance(event, dict):
                    return
                record_proxy_response_increment(event.get("encodedDataLength"))

            session.on("Network.dataReceived", _record_data_received)
            await asyncio.wait_for(
                session.send("Network.enable"),
                timeout=_CLEANUP_TIMEOUT_S,
            )
            return session
        except Exception:
            logger.warning("pjud_mint_transfer_telemetry_unavailable")
            if session is not None:
                await self._detach_cdp_session(session)
            return None

    @staticmethod
    async def _fence_cdp_callbacks(session) -> None:
        if session is None:
            return
        try:
            await asyncio.wait_for(
                session.send(
                    "Runtime.evaluate",
                    {"expression": "void 0", "returnByValue": True},
                ),
                timeout=_CLEANUP_TIMEOUT_S,
            )
        except Exception:
            logger.warning("pjud_mint_cdp_fence_unavailable")

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
            cdp_session = None

            def _record_started(request) -> None:
                try:
                    body = request.post_data_buffer or b""
                except Exception:
                    body = b""
                record_proxy_request(len(body))

            try:
                context = await browser.new_context()
                page = await context.new_page()
                # This fires as soon as Chromium starts a request, including
                # navigations that later timeout before CDP reports response bytes.
                page.on("request", _record_started)
                cdp_session = await self._create_cdp_meter(context, page)
                try:
                    await page.goto(
                        f"{self._base_url}{_CONSULTA_PATH}",
                        wait_until="domcontentloaded",
                        timeout=_MINT_TIMEOUT_MS,
                    )
                except PlaywrightError:
                    raise MintUnavailableError("navigation_failed") from None
                logger.info("PJUD navigation ready")
                try:
                    await page.wait_for_selector(
                        _FORM_READY_SELECTOR,
                        timeout=_MINT_TIMEOUT_MS,
                    )
                except PlaywrightError:
                    raise MintUnavailableError("form_timeout") from None
                ua = await page.evaluate("() => navigator.userAgent")
                pw_cookies = await context.cookies()
                cookies = playwright_cookie_records(pw_cookies)
                logger.info(
                    "PJUD form ready; cookie_count=%d has_php_session=%s has_ts_family=%s",
                    len(cookies), any(cookie.name == "PHPSESSID" for cookie in cookies),
                    any(cookie.name.startswith("TS") for cookie in cookies),
                )
                return MintResult(cookies=cookies, user_agent=ua)
            finally:
                try:
                    await self._fence_cdp_callbacks(cdp_session)
                finally:
                    cleanup = [self._close_playwright_resource(browser, "browser")]
                    if cdp_session is not None:
                        cleanup.append(self._detach_cdp_session(cdp_session))
                    await asyncio.gather(*cleanup)
