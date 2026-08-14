import asyncio
import logging
import time
from collections.abc import Mapping, Sequence

import httpx

from app.bandwidth import (
    estimate_request_bytes,
    record_proxy_request,
    record_proxy_response,
    record_proxy_retry,
)
from app.cookie_scope import (
    CookieRecord,
    cookie_jar_from_records,
    cookie_records_from_jar,
    legacy_cookie_records,
    legacy_cookie_scope,
)
from app.config import Settings

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class OJVHttpAdapter:
    def __init__(
        self,
        settings: Settings,
        proxy: str | None = None,
        user_agent: str | None = None,
        cookies: Sequence[CookieRecord] | Mapping[str, str] | None = None,
    ):
        self._settings = settings
        self._base = settings.OJV_BASE_URL.rstrip("/")
        self._rate_limit_s = settings.RATE_LIMIT_MS / 1000.0
        self._last_request_time: float = 0.0
        cookie_records = cookies or ()
        if isinstance(cookie_records, Mapping):
            domain, secure = legacy_cookie_scope(self._base)
            cookie_records = legacy_cookie_records(
                cookie_records, domain=domain, secure=secure,
            )
        self._client = httpx.AsyncClient(
            proxy=proxy,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            cookies=cookie_jar_from_records(cookie_records),
            headers={
                "User-Agent": user_agent or _USER_AGENT,
                "Accept-Language": "es-CL,es;q=0.9",
            },
        )

    async def _rate_limit(self):
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._rate_limit_s:
            await asyncio.sleep(self._rate_limit_s - elapsed)
        self._last_request_time = time.monotonic()

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self._request("get", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self._request("post", path, **kwargs)

    async def post_once(self, path: str, **kwargs) -> httpx.Response:
        """POST exactly once; opportunistic work must never amplify traffic."""
        # The shared client follows redirects for normal PJUD traffic. A 307/308
        # preserves POST, so following it here would be a hidden second request.
        kwargs["follow_redirects"] = False
        return await self._request("post", path, max_attempts=1, **kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        max_attempts: int = 2,
        **kwargs,
    ) -> httpx.Response:
        url = f"{self._base}{path}"
        request = getattr(self._client, method)
        for attempt in range(max_attempts):
            await self._rate_limit()
            logger.debug("%s %s", method.upper(), url)
            record_proxy_request(estimate_request_bytes(kwargs))
            try:
                response = await request(url, **kwargs)
            except httpx.TransportError:
                if attempt + 1 >= max_attempts:
                    raise
                record_proxy_retry()
                logger.warning("OJV transport failed; retrying once")
                continue
            record_proxy_response(len(response.content))
            return response

        raise AssertionError("unreachable")

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    def snapshot_cookies(self) -> tuple[CookieRecord, ...]:
        return cookie_records_from_jar(self._client.cookies.jar)

    async def close(self):
        await self._client.aclose()
