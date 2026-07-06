import asyncio
import logging
import time

from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.cookie_store import CookieStore
from app.minter import CookieMinter, MintResult
from app.session import OJVSession
from worker.config import WorkerConfig

logger = logging.getLogger(__name__)


async def get_or_mint_cookies(store, minter, max_age_s: int):
    """Devuelve cookies frescos del store, o mintea y persiste si faltan/expiraron."""
    bundle = store.load()
    if bundle is not None and bundle.age_seconds < max_age_s:
        return MintResult(cookies=bundle.cookies, user_agent=bundle.user_agent)
    result = await minter.mint()
    store.save(cookies=result.cookies, user_agent=result.user_agent)
    return result


class SessionPool:
    def __init__(self, config: WorkerConfig):
        self._config = config
        self._pool: list[OJVSession] = []
        self._semaphore = asyncio.Semaphore(config.POOL_SIZE)
        self._global_rate_lock = asyncio.Lock()
        self._last_global_request: float = 0.0
        self._global_min_delay: float = 1.2
        self._store = CookieStore(config.COOKIE_STORE_PATH)
        self._minter = CookieMinter(config.PJUD_BASE_URL)

    async def initialize(self):
        settings = Settings(
            API_KEY="unused-by-worker",
            OJV_BASE_URL=self._config.PJUD_BASE_URL,
            RATE_LIMIT_MS=self._config.RATE_LIMIT_MS,
        )
        for i in range(self._config.POOL_SIZE):
            creds = await get_or_mint_cookies(self._store, self._minter, self._config.SESSION_MAX_AGE_S)
            adapter = OJVHttpAdapter(
                settings,
                user_agent=creds.user_agent,
                cookies=creds.cookies,
            )
            session = OJVSession(adapter)
            await session.initialize()
            self._pool.append(session)
            logger.info("Session %d initialized", i)
            if i < self._config.POOL_SIZE - 1:
                await asyncio.sleep(1.5)  # stagger

    async def acquire(self) -> OJVSession:
        """Acquire a session from the pool.

        NOTE: Currently always returns the first session. This is correct for
        POOL_SIZE=1. For POOL_SIZE>1, implement proper available/in-use tracking.
        """
        await self._semaphore.acquire()
        for i, session in enumerate(self._pool):
            if session.age_seconds > self._config.SESSION_MAX_AGE_S:
                logger.info("Refreshing expired session %d (age=%.0fs)", i, session.age_seconds)
                try:
                    await self._refresh_session(session)
                except Exception:
                    # No penalizar la causa por un fallo de minteo/refresh: devolver la
                    # sesión existente (expirada). El challenge F5 que devuelva se detecta
                    # downstream y va por el path de bloqueo (sin incrementar sync_attempts),
                    # disparando el re-mint reactivo. El semáforo se libera normalmente en release().
                    logger.exception("Refresh de sesión %d falló; usando la sesión existente", i)
                return self._pool[i]
        return self._pool[0]

    def release(self, session: OJVSession):
        self._semaphore.release()

    async def enforce_global_rate_limit(self):
        async with self._global_rate_lock:
            elapsed = time.monotonic() - self._last_global_request
            if elapsed < self._global_min_delay:
                await asyncio.sleep(self._global_min_delay - elapsed)
            self._last_global_request = time.monotonic()

    async def _refresh_session(self, session: OJVSession):
        idx = self._pool.index(session)
        settings = Settings(
            API_KEY="unused-by-worker",
            OJV_BASE_URL=self._config.PJUD_BASE_URL,
            RATE_LIMIT_MS=self._config.RATE_LIMIT_MS,
        )
        creds = await get_or_mint_cookies(self._store, self._minter, self._config.SESSION_MAX_AGE_S)
        adapter = OJVHttpAdapter(
            settings,
            user_agent=creds.user_agent,
            cookies=creds.cookies,
        )
        new_session = OJVSession(adapter)
        # Init the NEW session before touching the old one: if this raises, the
        # old (still-open) session stays in the pool instead of a dead closed one.
        await new_session.initialize()
        self._pool[idx] = new_session
        await session.close()

    async def force_remint(self):
        """Fuerza un minteo fresco tras un bloqueo. Persiste al store y
        refresca las sesiones vivas del pool con los cookies nuevos.

        NOTA: seguro solo para POOL_SIZE=1 (consumidor único). Con POOL_SIZE>1
        haría falta un lock para no cerrar sesiones en uso por otras corrutinas.
        """
        result = await self._minter.mint()
        self._store.save(cookies=result.cookies, user_agent=result.user_agent)
        for session in list(self._pool):
            await self._refresh_session(session)

    async def close_all(self):
        for session in self._pool:
            await session.close()
        self._pool.clear()
