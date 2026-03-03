import asyncio
import logging
import time

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class OJVHttpAdapter:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._base = settings.OJV_BASE_URL.rstrip("/")
        self._rate_limit_s = settings.RATE_LIMIT_MS / 1000.0
        self._last_request_time: float = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "es-CL,es;q=0.9",
            },
        )

    async def _rate_limit(self):
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._rate_limit_s:
            await asyncio.sleep(self._rate_limit_s - elapsed)
        self._last_request_time = time.monotonic()

    async def get(self, path: str, **kwargs) -> httpx.Response:
        await self._rate_limit()
        url = f"{self._base}{path}"
        logger.debug("GET %s", url)
        return await self._client.get(url, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        await self._rate_limit()
        url = f"{self._base}{path}"
        logger.debug("POST %s", url)
        return await self._client.post(url, **kwargs)

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    async def close(self):
        await self._client.aclose()
